cmake_minimum_required(VERSION 3.16)
project(crashkiller_uploader LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

add_executable(crashkiller_uploader main.cpp)

if (WIN32)
    target_link_libraries(crashkiller_uploader PRIVATE winhttp)
endif()
