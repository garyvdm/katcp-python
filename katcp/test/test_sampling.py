"""Tests for the katcp.sampling module."""

import unittest
import time
import logging
import katcp
from katcp.testutils import TestLogHandler, DeviceTestSensor
from katcp import sampling

class TestSampling(unittest.TestCase):

    def setUp(self):
        """Set up for test."""
        # test sensor
        self.sensor = DeviceTestSensor(
                katcp.Sensor.INTEGER, "an.int", "An integer.", "count",
                [-4, 3],
                timestamp=12345, status=katcp.Sensor.NOMINAL, value=3
        )

        # test callback
        def inform(msg):
            self.calls.append(msg)

        self.calls = []
        self.inform = inform

    def test_sampling(self):
        """Test getting and setting the sampling."""
        s = self.sensor

        sampling.SampleNone(None, s)
        sampling.SampleAuto(None, s)
        sampling.SamplePeriod(None, s, 10)
        sampling.SampleEvent(None, s)
        sampling.SampleDifferential(None, s, 2)
        self.assertRaises(ValueError, sampling.SampleNone, None, s, "foo")
        self.assertRaises(ValueError, sampling.SampleAuto, None, s, "bar")
        self.assertRaises(ValueError, sampling.SamplePeriod, None, s)
        self.assertRaises(ValueError, sampling.SamplePeriod, None, s, "1.5")
        self.assertRaises(ValueError, sampling.SamplePeriod, None, s, "-1")
        self.assertRaises(ValueError, sampling.SampleEvent, None, s, "foo")
        self.assertRaises(ValueError, sampling.SampleDifferential, None, s)
        self.assertRaises(ValueError, sampling.SampleDifferential, None, s, "-1")
        self.assertRaises(ValueError, sampling.SampleDifferential, None, s, "1.5")

        sampling.SampleStrategy.get_strategy("none", None, s)
        sampling.SampleStrategy.get_strategy("auto", None, s)
        sampling.SampleStrategy.get_strategy("period", None, s, "15")
        sampling.SampleStrategy.get_strategy("event", None, s)
        sampling.SampleStrategy.get_strategy("differential", None, s, "2")
        self.assertRaises(ValueError, sampling.SampleStrategy.get_strategy, "random", None, s)
        self.assertRaises(ValueError, sampling.SampleStrategy.get_strategy, "period", None, s, "foo")
        self.assertRaises(ValueError, sampling.SampleStrategy.get_strategy, "differential", None, s, "bar")

    def test_event(self):
        """Test SampleEvent strategy."""
        event = sampling.SampleEvent(self.inform, self.sensor)
        self.assertEqual(self.calls, [])

        event.attach()
        self.assertEqual(len(self.calls), 1)

    def test_differential(self):
        """Test SampleDifferential strategy."""
        diff = sampling.SampleDifferential(self.inform, self.sensor, 5)
        self.assertEqual(self.calls, [])

        diff.attach()
        self.assertEqual(len(self.calls), 1)

    def test_periodic(self):
        """Test SamplePeriod strategy."""
        # period = 10s
        period = sampling.SamplePeriod(self.inform, self.sensor, 10000)
        self.assertEqual(self.calls, [])

        period.attach()
        self.assertEqual(self.calls, [])

        period.periodic(1)
        self.assertEqual(len(self.calls), 1)

        period.periodic(11)
        self.assertEqual(len(self.calls), 2)

        period.periodic(12)
        self.assertEqual(len(self.calls), 2)


class TestReactor(unittest.TestCase):

    def setUp(self):
        """Set up for test."""
        # test sensor
        self.sensor = DeviceTestSensor(
                katcp.Sensor.INTEGER, "an.int", "An integer.", "count",
                [-4, 3],
                timestamp=12345, status=katcp.Sensor.NOMINAL, value=3
        )

        # test callback
        def inform(msg):
            self.calls.append(msg)

        # test reactor
        self.reactor = sampling.SampleReactor()
        self.reactor.start()

        self.calls = []
        self.inform = inform

    def tearDown(self):
        """Clean up after test."""
        self.reactor.stop()
        self.reactor.join(1.0)

    def test_periodic(self):
        """Test reactor with periodic sampling."""
        period = sampling.SamplePeriod(self.inform, self.sensor, 10)
        self.reactor.add_strategy(period)
        time.sleep(0.1)
        self.reactor.remove_strategy(period)

        self.assertTrue(10 <= len(self.calls) <= 11, "Expect 9 to 11 informs, got %s" % len(self.calls))