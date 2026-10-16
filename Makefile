.PHONY: test test-failure

test:
	PYTHONPATH=$(CURDIR)/src python3 -m unittest discover -s tests

test-failure:
	PYTHONPATH=$(CURDIR)/src python3 -m unittest discover -s tests -p 'test_storage.py'
	PYTHONPATH=$(CURDIR)/src python3 -m unittest discover -s tests -p 'test_verification.py'
