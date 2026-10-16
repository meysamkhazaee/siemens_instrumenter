if(NOT DEFINED COVERAGE_BUILD_DIR)
    message(FATAL_ERROR "COVERAGE_BUILD_DIR is not defined")
endif()

file(
    GLOB_RECURSE
    GCDA_FILES
    "${COVERAGE_BUILD_DIR}/*.gcda"
)

if(GCDA_FILES)

    list(LENGTH GCDA_FILES GCDA_COUNT)

    message(
        STATUS
        "Removing ${GCDA_COUNT} old coverage data file(s)"
    )

    file(REMOVE ${GCDA_FILES})

else()

    message(
        STATUS
        "No old .gcda coverage files found"
    )

endif()