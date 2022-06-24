#define PY_SSIZE_T_CLEAN
#include "common.h"

int main(int argc, char *argv[]) {
  int ret = update_pyhome();
  return run_script("salt_run", argv[0]);
}
