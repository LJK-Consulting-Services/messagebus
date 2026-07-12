"""Unit tests for the argparse validators in the extensionless ``bus`` script."""
import argparse
import importlib.machinery
import importlib.util
import os
import unittest


_BUS_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "bus")


def _load_bus():
    loader = importlib.machinery.SourceFileLoader("bus_validators", _BUS_PATH)
    spec = importlib.util.spec_from_loader("bus_validators", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


bus = _load_bus()


class IdentTests(unittest.TestCase):
    def test_accepts_safe_identifier_characters_unchanged(self):
        value = "aZ09._-"

        self.assertEqual(bus.ident(value), value)

    def test_rejects_unsafe_or_empty_identifiers(self):
        for value in ("agent/id", "agent id", "agent:id", ""):
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    bus.ident(value)


class PositiveIntTests(unittest.TestCase):
    def test_accepts_one_and_larger_integers(self):
        self.assertEqual(bus.positive_int("1"), 1)
        self.assertEqual(bus.positive_int("42"), 42)

    def test_rejects_zero_negative_nonnumeric_and_empty_values(self):
        for value in ("0", "-1", "1.5", "one", ""):
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    bus.positive_int(value)


if __name__ == "__main__":
    unittest.main()
