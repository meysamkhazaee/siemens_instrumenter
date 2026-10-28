########################################################################

## Linux usage
First make sure Clang is installed. On Debian/Ubuntu, for example:
sudo apt install clang

## Then:
./instrumenter sample.cpp -d output

or:
./instrumenter sample.cpp -d output --compiler /usr/bin/clang++

## Compile:
g++ output/sample_instrumented.cpp -o output/sample_instrumented

## Run:
./output/sample_instrumented.exe --p-output output/test2.txt 1 2 3 4 5 6 7 8

########################################################################


########################################################################

## to create the exe use below command:
python build.py

########################################################################

# after build exe:
# Windows usage
instrumenter.exe sample.cpp -d output

# or with explicit Clang:
instrumenter.exe sample.cpp -d output --compiler C:/msys64/ucrt64/bin/clang++.exe

# Then compile the generated source:
g++ output/sample_instrumented.cpp -o output/sample_instrumented.exe

########################################################################


########################################################################

## Single Siemens file:
./dist/instrumenter.exe \
  ./input/siemens/printtokens/versions.alt/versions.orig/v1/print_tokens.c \
  -d ./output/siemens

# Extra Clang flags go after --
./dist/instrumenter.exe ./input/siemens/replace/source.alt/source.orig/replace.c \
  -d ./output/siemens -- -std=c89

########################################################################


########################################################################

## Whole Siemens suite (run_siemens.py)
# Instruments and compiles every C file under input/siemens
# (test-data folders like inputs/ are skipped).
#
# Stage 1: instrument every source with the instrumenter exe
# Stage 2: compile every instrumented source, exe is placed next to it
#
# Output mirrors the Siemens layout under output/siemens_instrumented:
#
#   input/siemens/tcas/versions.alt/versions.orig/v1/tcas.c
#   -> output/siemens_instrumented/tcas/versions.alt/versions.orig/v1/
#        tcas_instrumented.c
#        tcas_instrumented.exe      (no .exe suffix on Linux)
#        tcas_instrumenter.log      (stage 1 output)
#        tcas_compile.log           (stage 2 output)
#        *.h                        (headers copied from the source dir)

python run_siemens.py                       # both stages
python run_siemens.py --skip-compile        # stage 1 only
python run_siemens.py --skip-instrument     # stage 2 only
python run_siemens.py --only tcas schedule  # some programs only
python run_siemens.py --exe dist/instrumenter.exe
python run_siemens.py --cc clang            # default: gcc, then clang
python run_siemens.py --dry-run             # print commands, run nothing
python run_siemens.py --commands-file run_all.sh    # or run_all.bat

# The instrumenter exe is searched in:
#   dist/instrumenter.exe, dist/instrumenter, linux_exe/instrumenter
# Rebuild it (python build.py) after changing instrumenter.py.

# Compile flags:
#   all programs : -std=gnu89 -lm   (old K&R C)
#   replace      : -std=c89 -lm     (replace.c defines its own getline(),
#                                    which clashes with modern <stdio.h>)

## Run an instrumented Siemens program:
./output/siemens_instrumented/tcas/source.alt/source.orig/tcas_instrumented.exe \
  --p-output trace.txt 958 1 1 2597 574 4253 0 399 400 0 0 1

########################################################################
