from setuptools import setup

setup(
    name='sem-scale-bar',
    version='1.0.0',
    description='Automatically detect and overlay publication-quality scale bars on SEM images',
    py_modules=['process_sem_images'],
    python_requires='>=3.8',
    install_requires=[
        'opencv-python>=4.8',
        'numpy>=1.24',
        'pytesseract>=0.3.10',
        'Pillow>=9.0',
    ],
    license='MIT',
)
