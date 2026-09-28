from setuptools import Extension, setup

setup(
    name='fastaudio',
    ext_modules=[Extension('fastaudio', sources=['fastaudio.c'], extra_compile_args=['-O3', '-Wall'], libraries=['m'])],
)
