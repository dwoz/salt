PWD := $(shell pwd)
include config.mk
PYTHON_VERSION ?= 3.8.13
PY_SUFFIX ?= $(shell echo $(PYTHON_VERSION) | sed -r 's/([0-9]+)(\.[0-9]+)(\.[0-9]+)/\1\2/')
SCRIPTS ?= salt salt-api salt-call salt-cloud salt-cp salt-key salt-master salt-minion salt-proxy salt-run salt-ssh salt-syndic spm
TARGET_DIR ?= $(PWD)/build/salt
TARGET_DIRNAME := $(shell dirname $(TARGET_DIR))
TARGET_BASENAME := $(shell basename $(TARGET_DIR))
#SOURCE := ../scripts/c

#.PHONY: all clean onedir clean_salt $(SCRIPTS) install_salt
.PHONY: all

all: $(TARGET_DIRNAME)/Python-$(PYTHON_VERSION) #$(TARGET_DIR)/bin/salt

onedir: salt.tar.xz

$(TARGET_DIR):
	mkdir -p $(TARGET_DIR)

$(TARGET_DIRNAME)/Python-$(PYTHON_VERSION).tar.xz: $(TARGET_DIR)
	curl https://www.python.org/ftp/python/$(PYTHON_VERSION)/Python-$(PYTHON_VERSION).tar.xz -o $(TARGET_DIRNAME)/Python-$(PYTHON_VERSION).tar.xz

$(TARGET_DIRNAME)/Python-$(PYTHON_VERSION): $(TARGET_DIRNAME)/Python-$(PYTHON_VERSION).tar.xz
	cd $(TARGET_DIRNAME); \
	tar xvf Python-$(PYTHON_VERSION).tar.xz; \
	cd $(PWD)

$(TARGET_DIR)/bin/python$(PY_SUFFIX):  $(TARGET_DIRNAME)/Python-$(PYTHON_VERSION)
	cd $(TARGET_DIRNAME)/Python-$(PYTHON_VERSION); \
	./configure --prefix=$(TARGET_DIR) --enable-optimizations; \
	make -j4; make install;

$(TARGET_DIR)/bin/salt: $(TARGET_DIR)/bin/python$(PY_SUFFIX)
	$(TARGET_DIR)/bin/pip$(PY_SUFFIX) install .

# XXX: Make this salt-pip instead?
$(TARGET_DIR)/bin/pip3: $(TARGET_DIR)/bin/python$(PY_SUFFIX)
	rm -r $(TARGET_DIR)/bin/pip3;
	cp $(PWD)/scripts/pip3 $(TARGET_DIR)/bin/pip3

$(SCRIPTS): $(TARGET_DIR)/bin/pip3
	cd $(TARGET_DIR); \
	cp bin/$@ ./; \
	cd ../..
	sed -i 's/^#!.*$$/#!\/bin\/sh\n"exec" "`dirname $$0`\/bin\/python3" "$$0" "$$@"/' build/salt/$@;

salt.tar.xz: $(SCRIPTS)
	py3clean $(TARGET_DIR)
	# XXX: Should we keep this?
	rm -rf $(TARGET_DIR)/include $(TARGET_DIR)/share
	tar cJvf salt.tar.xz -C $(TARGET_DIRNAME) $(TARGET_BASENAME);

# $(SCRIPTS): $(TARGET_DIR)/bin/salt
# 	cd build/salt; \
# 	cp bin/$@ ./; \
# 	cd ../..
# 	sed -i 's/^#!.*$$/#!\/bin\/sh\n"exec" "`dirname $$0`\/bin\/python3" "$$0" "$$@"/' build/salt/$@;
# 
# onedir_clean: $(SCRIPTS)
# 	py3clean build/salt
# 	# XXX: Should we keep this?
# 	rm -rf salt/include salt/share
# 	cd build/salt/bin; rm -f $(SCRIPTS); cd ../../..

# all: salt/bin/pip$(PY_SUFFIX) salt/bin/pip3 install_salt $(SCRIPTS) py3clean
# 
# 
# all: build/Makefile
# 	@$(MAKE) -C build
# 
# onedir: build/salt.tar.xz
# 
# build/salt.tar.xz: build/Makefile
# 	@$(MAKE) -C build
# 	cd build; tar cJvf salt.tar.xz salt; cd ..
# 
# build/Makefile: build Makefile.build
# 	cp $(PWD)/Makefile.build $(PWD)/build/Makefile
# 
# build:
# 	mkdir $(PWD)/build
# 
# clean:
# 	rm -rf build
# 
# #clean:
# #	rm $(BIN_NAMES) *.so
# 
# #clean_salt:
# #	rm common.o salt.o salt salt-master.o salt-master salt-minion.o salt-minion
# 
# #common.o:
# #	gcc -std=gnu99 $(shell $(PWD)/python/bin/python3.8-config --cflags) -c $(SOURCE)/common.c -o common.o
# #
# ## XXX: Simplify this
# #$(BIN_NAMES): common.o
# #	gcc -std=gnu99 $(shell $(PWD)/python/bin/python3.8-config --cflags) -c $(SOURCE)/$@.c -o $@.o
# #	gcc common.o $@.o -Wl,-rpath,'$$ORIGIN' $(shell $(PWD)/python/bin/python3.8-config --ldflags --embed) -rdynamic -o $@
