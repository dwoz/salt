import collections.abc
import contextvars

loader_ctxvar = contextvars.ContextVar('loader_ctxvar')


class NamedLoaderContext(collections.abc.MutableMapping):

    def __init__(self, name, loader_context):
        self.name = name
        self.loader_context = loader_context

    @property
    def loader(self):
        return self.loader_context.loader

    def value(self):
        if self.name == '__context__':
            return self.loader.pack[self.name]
        if self.name == self.loader.pack_self:
            return self.loader
        return self.loader.pack[self.name]

    @property
    def eldest_loader(self):
        loader = self.loader
        while loader.parent_loader is not None:
            loader = loader.parent_laoder
        return loader

    def get(self, item, default=None):
        if self.name == '__context__':
            return self.loader.pack[self.name].get(item, default)
        if self.name == self.loader.pack_self:
            return self.loader.get(item, default)
        return self.loader.pack[self.name].get(item, default)

    def __getitem__(self, item):
        if self.name == '__context__':
            return self.loader.pack[self.name][item]
        if self.name == self.loader.pack_self:
            return self.loader[item]
        return self.loader.pack[self.name][item]

    def __contains__(self, item):
        if self.name == '__context__':
            print(repr(self.loader.pack[self.name]))
            return item in self.loader.pack[self.name]
        if self.name == self.loader.pack_self:
            return item in self.loader
        return item in self.loader.pack[self.name]

    def __setitem__(self, item, value):
        if self.name == '__context__':
            self.loader.pack[self.name][item] = value
        if self.name == self.loader.pack_self:
            self.loader[self.name] = value
        self.loader.pack[self.name][item] = value

    def __len__(self):
        if self.name == '__context__':
            return self.loader.pack[self.name].__len__()
        if self.name == self.loader.pack_self:
            return self.loader.__len__()
        return self.loader.pack[self.name].__len__()

    def __iter__(self):
        if self.name == '__context__':
            return self.loader.pack[self.name].__iter__()
        if self.name == self.loader.pack_self:
            return self.loader.__iter__()
        return self.loader.pack[self.name].__iter__()

    def __delitem__(self, item):
        if self.name == '__context__':
            return self.loader.pack[self.name].__delitem__(item)
        if self.name == self.loader.pack_self:
            return self.loader.__delitem__(item)
        return self.loader.pack[self.name].__delitem__(item)

    def __eq__(self, other):
        if not isinstance(other, self.__class__):
            return False
        return (
            self.loader_context == other.loader_context and
            self.name == other.name
        )


class LoaderContext(object):

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
