"""
Manage the context a module loaded by salt's loader
"""
import collections.abc
import contextlib
import contextvars

loader_ctxvar = contextvars.ContextVar("loader_ctxvar")


@contextlib.contextmanager
def loader_context(loader):
    """
    A context manager that sets and un-sets the loader context
    """
    tok = loader_ctxvar.set(loader)
    try:
        yield
    finally:
        loader_ctxvar.reset(tok)


class NamedLoaderContext(collections.abc.MutableMapping):
    """
    A NamedLoaderContext object is injected by the loader providing access to
    Salt's 'magic dunders' (__salt__, __utils__, ect).
    """

    def __init__(self, name, loader_context):
        self.name = name
        self.loader_context = loader_context

    @property
    def loader(self):
        return self.loader_context.loader

    def value(self):
        if self.name == "__context__":
            return self.loader.pack[self.name]
        if self.name == self.loader.pack_self:
            return self.loader
        return self.loader.pack[self.name]

    @property
    def eldest_loader(self):
        loader = self.loader
        while loader.parent_loader is not None:
            loader = loader.parent_loader
        return loader

    def get(self, key, default=None):
        if self.name == "__context__":
            return self.loader.pack[self.name].get(key, default)
        if self.name == self.loader.pack_self:
            return self.loader.get(key, default)
        return self.loader.pack[self.name].get(key, default)

    def __getitem__(self, item):
        if self.name == "__context__":
            return self.loader.pack[self.name][item]
        if self.name == self.loader.pack_self:
            return self.loader[item]
        return self.loader.pack[self.name][item]

    def __contains__(self, item):
        if self.name == "__context__":
            return item in self.loader.pack[self.name]
        if self.name == self.loader.pack_self:
            return item in self.loader
        return item in self.loader.pack[self.name]

    def __setitem__(self, item, value):
        if self.name == "__context__":
            self.loader.pack[self.name][item] = value
        if self.name == self.loader.pack_self:
            self.loader[self.name] = value
        self.loader.pack[self.name][item] = value

    def __len__(self):
        if self.name == "__context__":
            return self.loader.pack[self.name].__len__()
        if self.name == self.loader.pack_self:
            return self.loader.__len__()
        return self.loader.pack[self.name].__len__()

    def __iter__(self):
        if self.name == "__context__":
            return self.loader.pack[self.name].__iter__()
        if self.name == self.loader.pack_self:
            return self.loader.__iter__()
        return self.loader.pack[self.name].__iter__()

    def __delitem__(self, item):
        if self.name == "__context__":
            return self.loader.pack[self.name].__delitem__(item)
        if self.name == self.loader.pack_self:
            return self.loader.__delitem__(item)
        return self.loader.pack[self.name].__delitem__(item)

    def __eq__(self, other):
        if not isinstance(other, self.__class__):
            return False
        return self.loader_context == other.loader_context and self.name == other.name

    def __getstate__(self):
        return {"name": self.name, "loader_context": self.loader_context}

    def __setstate__(self, state):
        self.name = state["name"]
        self.loader_context = state["loader_context"]

    def __getattr__(self, name):
        if self.name == "__context__":
            return getattr(self.loader.pack[self.name], name)
        if self.name == self.loader.pack_self:
            return getattr(self.loader, name)
        return getattr(self.loader.pack[self.name], name)


class LoaderContext:
    """
    A loader context object, this object is injected at <loaded
    module>.__salt_loader__ by the salt-loader. It is responsible for providing
    access to the current context's loader
    """

    def __init__(self, loader_ctxvar=loader_ctxvar):
        self.loader_ctxvar = loader_ctxvar

    def __getitem__(self, item):
        return self.loader[item]

    @property
    def loader(self):
        return self.loader_ctxvar.get()

    def named_context(self, name, ctx_class=NamedLoaderContext):
        return ctx_class(name, self)

    def __eq__(self, other):
        if not isinstance(other, self.__class__):
            return False
        return self.loader_ctxvar == other.loader_ctxvar

    def __getstate__(self):
        return {"varname": self.loader_ctxvar.name}

    def __setstate__(self, state):
        self.loader_ctxvar = contextvars.ContextVar(state["varname"])
