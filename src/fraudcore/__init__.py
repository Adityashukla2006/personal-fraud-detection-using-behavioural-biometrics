"""Decision logic shared by the Lambda handlers and the research track.

Pure Python by hard constraint: standard library only, plus boto3 in the handlers that need it.
No numpy, pandas, scikit-learn or scipy may ever be imported from here. See
``tests/unit/test_runtime_purity.py``, which fails if that rule is broken.
"""
