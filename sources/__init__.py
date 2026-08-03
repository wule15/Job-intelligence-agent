"""
Job sources. One module per provider.

Each returns a list of job dicts and is expected to fail on its own without
ending the run. job_search_smart.py wraps every call, times it and records
the outcome.
"""
