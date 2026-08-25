"""
Test runner that bypasses the broken web3 pytest plugin by running tests directly.
"""
import sys
import traceback
import importlib
import unittest

TESTS = [
    "tests.test_detector",
    "tests.test_agents",
    "tests.test_audit",
]

passed = 0
failed = 0
errors = []

print("=" * 60)
print("  RAZORPAY AI REVENUE RECOVERY — TEST RUNNER")
print("=" * 60)

for test_module_name in TESTS:
    try:
        module = importlib.import_module(test_module_name)
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromModule(module)
        runner = unittest.TextTestRunner(verbosity=2, stream=sys.stdout)
        result = runner.run(suite)
        passed += result.testsRun - len(result.failures) - len(result.errors)
        failed += len(result.failures) + len(result.errors)
        for f in result.failures + result.errors:
            errors.append(f)
    except Exception as e:
        print(f"ERROR loading {test_module_name}: {e}")
        traceback.print_exc()
        failed += 1

print()
print("=" * 60)
print(f"  RESULTS: {passed} passed, {failed} failed")
print("=" * 60)

sys.exit(0 if failed == 0 else 1)
