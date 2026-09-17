import unittest

from service.main import Handler


class HealthContractTest(unittest.TestCase):
    def test_handler_is_available(self):
        self.assertTrue(hasattr(Handler, "do_GET"))


if __name__ == "__main__":
    unittest.main()
