cmake_minimum_required(VERSION 3.20)

project(MatrixCoverage LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 11)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_executable(sample sample.cpp)

option(ENABLE_COVERAGE "Enable GCC code coverage" OFF)

if(ENABLE_COVERAGE)

    if(NOT CMAKE_CXX_COMPILER_ID STREQUAL "GNU")
        message(FATAL_ERROR "Coverage requires GCC/G++")
    endif()

    target_compile_options(sample PRIVATE
        -O0
        -g
        --coverage
    )

    target_link_options(sample PRIVATE
        --coverage
    )

endif()


# ============================================================
# Tests
# ============================================================

enable_testing()

add_test(
    NAME Test1_X1_NotInvertible
    COMMAND sample 1 2 2 4 1 0 0 1
)

add_test(
    NAME Test2_X2_NotInvertible
    COMMAND sample 1 0 0 1 1 2 2 4
)

add_test(
    NAME Test3_NotInverse
    COMMAND sample 1 2 3 4 5 6 7 8
)

add_test(
    NAME Test4_Inverse
    COMMAND sample 1 0 0 1 1 0 0 1
)

add_test(
    NAME Test5_InvalidArguments
    COMMAND sample
)

add_test(NAME Test6_Product_NotInvertible
    COMMAND sample 65536 0 0 1 65536 0 0 1)
    
set_tests_properties(
    Test5_InvalidArguments
    PROPERTIES WILL_FAIL TRUE
)


# ============================================================
# Coverage report
# ============================================================

if(ENABLE_COVERAGE)

    find_program(
        GCOVR_EXECUTABLE
        NAMES gcovr.exe gcovr
        HINTS
            "C:/msys64/ucrt64/bin"
        REQUIRED
    )

    find_program(
        GCOV_EXECUTABLE
        NAMES gcov.exe gcov
        HINTS
            "C:/msys64/ucrt64/bin"
        REQUIRED
    )

    add_custom_target(
        coverage_report

        # Remove old .gcda files first
        COMMAND
            ${CMAKE_COMMAND}
            "-DCOVERAGE_BUILD_DIR=${CMAKE_BINARY_DIR}"
            -P
            "${CMAKE_SOURCE_DIR}/cmake/CleanCoverage.cmake"

        # Run all tests
        COMMAND
            ${CMAKE_CTEST_COMMAND}
            --test-dir "${CMAKE_BINARY_DIR}"
            --output-on-failure

        # Generate HTML report
        COMMAND
            ${GCOVR_EXECUTABLE}
            --root "${CMAKE_SOURCE_DIR}"
            --object-directory "${CMAKE_BINARY_DIR}"
            --gcov-executable "${GCOV_EXECUTABLE}"
            --html-details
            "${CMAKE_BINARY_DIR}/coverage.html"
            --print-summary

        DEPENDS sample

        WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}"

        COMMENT
            "Running tests and generating coverage report..."

        VERBATIM
    )

endif()