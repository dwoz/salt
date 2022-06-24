#define PY_SSIZE_T_CLEAN
#include <libgen.h>
#include <limits.h>
#include <stddef.h>
#include <stdio.h>
#include <Python.h>

int update_pyhome();
int run_script(char *name, char *programname);
