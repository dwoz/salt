#define PY_SSIZE_T_CLEAN
#include <libgen.h>
#include <limits.h>
#include <stddef.h>
#include <stdio.h>
#include <Python.h>


int update_pyhome() {

  char *buf, *newval, *oldval;
  if (getenv("PYTHONHOME") == NULL) {
    buf = malloc(PATH_MAX + 1);
    if (buf == NULL) {
      perror("malloc");
      exit(EXIT_FAILURE);
    }
    newval = malloc(PATH_MAX + sizeof(char) * 19);
    if (buf == NULL) {
      perror("malloc");
      exit(EXIT_FAILURE);
    }
    readlink("/proc/self/exe", buf, PATH_MAX);
    oldval = dirname(buf);
    sprintf(newval, "PYTHONHOME=%s/python", oldval);
    putenv(newval);

    free(buf);
    free(oldval);
    free(newval);
  }
  return 0;
}

int run_script(char *name, char *programname) {
  wchar_t *program = Py_DecodeLocale(programname, NULL);
  char *script;

  script = malloc(sizeof(char) * sizeof(name) + 500);
  if (script == NULL) {
    perror("malloc");
    exit(EXIT_FAILURE);
  }
  sprintf(script, "from salt.scripts import %s\n%s()\n", name, name);

  if (program == NULL) {
    fprintf(stderr, "Fatal error: cannot decode argv[0]\n");
    exit(1);
  }
  Py_SetProgramName(program); /* optional but recommended */
  Py_Initialize();
  PyRun_SimpleString(script);
  if (Py_FinalizeEx() < 0) {
    exit(120);
  }
  PyMem_RawFree(program);
  return 0;
}
