# Define minimal required version of CMake.
cmake_minimum_required(VERSION "3.25")

# Project definition
project(
	bt_rcp_freertos
	VERSION 1.0
	LANGUAGES C CXX ASM
)

# Include the definition of the slc_bt_rcp_freertos target,
# which contains the content of the SLC project
include(bt_rcp_freertos.cmake)

add_executable(bt_rcp_freertos
    # Add additional sources here
)

target_include_directories(bt_rcp_freertos PUBLIC
    # Add additional include paths here
)

target_compile_definitions(bt_rcp_freertos PUBLIC
    # Add additional macros here
)

target_compile_options(bt_rcp_freertos PUBLIC
    # Set additional compiler flags here
)

target_link_options(bt_rcp_freertos PUBLIC
    # Set additional linker flags here
)

# Link with the content defined in the SLC project
target_link_libraries(bt_rcp_freertos PRIVATE
    slc_bt_rcp_freertos
)

# Include managed project content if available
include(bt_rcp_freertos_project.cmake OPTIONAL RESULT_VARIABLE managed_project)
if(managed_project)
    message(STATUS "Using managed project content from ${managed_project}")
endif()

# Force the gcc linker command
set_target_properties(bt_rcp_freertos PROPERTIES LINKER_LANGUAGE C)

# Create .bin, .hex and .s37 artifacts after building the project
add_custom_command(TARGET bt_rcp_freertos
    POST_BUILD
    COMMAND ${CMAKE_OBJCOPY} -O srec "$<TARGET_FILE:bt_rcp_freertos>" "$<TARGET_FILE_DIR:bt_rcp_freertos>/$<TARGET_FILE_BASE_NAME:bt_rcp_freertos>.s37"
    COMMAND ${CMAKE_OBJCOPY} -O ihex "$<TARGET_FILE:bt_rcp_freertos>" "$<TARGET_FILE_DIR:bt_rcp_freertos>/$<TARGET_FILE_BASE_NAME:bt_rcp_freertos>.hex"
    COMMAND ${CMAKE_OBJCOPY} -O binary "$<TARGET_FILE:bt_rcp_freertos>" "$<TARGET_FILE_DIR:bt_rcp_freertos>/$<TARGET_FILE_BASE_NAME:bt_rcp_freertos>.bin" 
)

# Run post-build pipeline to perform additional post-processing
if(post_build_command)
add_custom_command(TARGET bt_rcp_freertos
    POST_BUILD
    WORKING_DIRECTORY ${CMAKE_CURRENT_LIST_DIR}/..
    COMMAND ${post_build_command}
)
endif()