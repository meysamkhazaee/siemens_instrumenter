########################################################################
# C / C++ Basic-Block Instrumenter & Siemens Benchmark Suite Runner
########################################################################

## Linux usage:
First make sure Clang is installed. On Debian/Ubuntu, for example:
  sudo apt install clang

Then:
  ./instrumenter sample.cpp -d output
or:
  ./instrumenter sample.cpp -d output --compiler /usr/bin/clang++

Compile:
  g++ output/sample.cpp -o output/sample.exe

Run:
  ./output/sample.exe --p-output output/test2.txt 1 2 3 4 5 6 7 8

########################################################################

## Build standalone executable (dist/instrumenter.exe):
  python build.py

########################################################################

## Windows usage:
  instrumenter.exe sample.cpp -d output
or with explicit Clang:
  instrumenter.exe sample.cpp -d output --compiler C:/msys64/ucrt64/bin/clang++.exe

Then compile the generated source:
  g++ output/sample.cpp -o output/sample.exe

########################################################################

## Batch Instrument Siemens Suite:
To mirror the input/siemens structure, instrument all healthy and faulty
versions, and compile all .exe binaries:
  python batch_instrument.py

Directory structure in output/siemens/<repo>/:
  - source/               : Error-free version (instrumented .c, compiled .exe, headers)
  - source.alt/source.orig: Error-free version (instrumented .c, compiled .exe, headers)
  - source.alt/v1..vN/    : Faulty versions (instrumented .c, compiled .exe, headers)
  - versions.alt/versions.orig/v1..vN/: Traditional Siemens layout (instrumented .c, compiled .exe)
  - inputs/               : Test input files
  - scripts/              : Test execution scripts (e.g. runall.sh)
  - testplans.alt/        : Test plans and universe file

Summary report:
  output/siemens/instrumentation_summary.json

########################################################################

## Test Scenario Execution & Dataset Generation:
To run tests across versions, detect failing and coincidentally correct
tests, and generate trace datasets:

  # Run on a specific version (all universe tests):
  python run_test_scenarios.py --repo tcas --version v1

  # Run with test limit (e.g. first 50 tests):
  python run_test_scenarios.py --repo schedule --version v3 --max-tests 50

  # Run all versions of a repository:
  python run_test_scenarios.py --repo schedule --version all

  # Run all 7 repositories and all 139 versions with 8 worker threads:
  python run_test_scenarios.py --repo all --version all -j 8

Output datasets are generated in:
  output/datasets/<repo>/

########################################################################

## Differences Between _dataset.txt and _tagged.txt:

For each tested version (e.g. tcas v1), two dataset text files are generated.
In both files, lines 1 through N are 100% IDENTICAL: each line represents
the execution trace (the sequence of P-points visited) for that test case.

The ONLY difference is in the FINAL LINE (the test verdict vector):

1) <version>_dataset.txt (Binary Pass / Fail):
   The final line contains only 'T' (Pass) and 'F' (Fail).
     - 'F' : Fail (the test output differed from the oracle output).
     - 'T' : Pass (the test output matched the oracle output, regardless
             of whether the fault site was executed or not).

   Use case: Ideal for standard machine learning classifiers, neural FL,
   and traditional Spectrum-Based Fault Localization (SBFL) algorithms
   (such as Ochiai, Tarantula, DStar) that require a binary Pass/Fail label.

2) <version>_tagged.txt (3-State with Coincidental Correctness Tag):
   The final line classifies tests into 3 categories: 'T', 'CC', and 'F'.
     - 'F'  : Fail (the fault was executed and caused output deviation).
     - 'CC' : Coincidentally Correct (the fault site was EXECUTED in the
              trace, but the test PASSED because the fault was masked and
              did not propagate to the output).
     - 'T'  : Clean Pass (the test passed and its execution path NEVER
              visited the fault site).

   Use case: Ideal for research on test suite quality, coincidental correctness
   noise reduction, and advanced fault localization filtering techniques.

Example Comparison of the Final Line (10-test sample):
  In _dataset.txt:
    T,T,T,F,T,F,F,T,T,T

  In _tagged.txt (tests #2 and #5 executed the fault but passed):
    T,CC,T,F,CC,F,F,T,T,T

########################################################################
