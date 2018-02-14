def setup_cython():
    from distutils.core import setup
    from distutils.extension import Extension
    from Cython.Distutils import build_ext
    import numpy
    ext_modules = []
    # malis pyx
    ext_modules += [Extension("em.lib.malis.malis_core", 
                             sources=["em/lib/malis/malis_core.pyx"], 
                             language='c++',extra_link_args=["-std=c++11"],
                             extra_compile_args=["-std=c++11", "-w"])]
    # warping pyx
    ext_modules += [Extension('em.lib.elektronn._warping',
                             sources=['em/lib/elektronn/_warping.pyx'],
                             extra_compile_args=['-std=c99', '-fno-strict-aliasing', '-O3', '-Wall', '-Wextra'])]
    setup(name='em_python',
       version='1.0',
       cmdclass = {'build_ext': build_ext}, 
       include_dirs=[numpy.get_include(),''], 
       packages=['em',
                 'em.lib','em.data', 'em.model',
                 'em.prune','em.quant','em.util',
                 'em.lib.malis', 'em.lib.elektronn',
                 'em.lib/align_affine'],
       ext_modules = ext_modules)
if __name__=='__main__':
    # export CPATH=$CONDA_PREFIX/include:$CONDA_PREFIX/include/python2.7/ 
    # pip install --editable .
	setup_cython()
