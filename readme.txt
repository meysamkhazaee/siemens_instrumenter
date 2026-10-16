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



./dist/instrumenter.exe \
  ./input/siemens/printtokens_2.0/printtokens/versions.alt/versions.orig/v1/print_tokens.c \
  -d ./output/siemens
