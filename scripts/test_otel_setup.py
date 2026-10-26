import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import otel_setup


class TestOtelSetup(unittest.TestCase):
    def test_apply_gcp_keeps_other_settings(self):
        settings = {"model": "sonnet", "env": {"FOO": "1", "OTEL_RESOURCE_ATTRIBUTES": "team=ce"}}
        cfg = {"otelEndpoint": "https://collector.example", "otelGcpProject": "cloudeng-agents-nonprod",
               "otelInstance": "my host,1"}
        out = otel_setup.apply(settings, cfg, helper="/h")
        env = out["env"]
        self.assertEqual(out["model"], "sonnet")
        self.assertEqual(env["FOO"], "1")
        self.assertEqual(env["OTEL_EXPORTER_OTLP_ENDPOINT"], "https://collector.example")
        self.assertEqual(env["OTEL_EXPORTER_OTLP_PROTOCOL"], "http/protobuf")
        self.assertEqual(env["OTEL_RESOURCE_ATTRIBUTES"],
                         "team=ce,gcp.project_id=cloudeng-agents-nonprod,cloud.region=us-central1,"
                         "service.instance.id=my-host-1")
        self.assertEqual(out["otelHeadersHelper"], "/h")
        self.assertNotIn("otelHeadersHelper", settings)  # input not mutated
        self.assertNotIn("OTEL_EXPORTER_OTLP_ENDPOINT", settings["env"])

    def test_apply_requires_endpoint_and_http_for_gcloud(self):
        with self.assertRaises(ValueError):
            otel_setup.apply({}, {})
        with self.assertRaises(ValueError):
            otel_setup.apply({}, {"otelEndpoint": "x", "otelProtocol": "grpc", "otelAuth": "gcloud"})

    def test_traces_toggle(self):
        cfg = {"otelEndpoint": "https://a"}
        on = otel_setup.apply({}, {**cfg, "otelTraces": "true"}, helper="/h")
        self.assertEqual(on["env"]["OTEL_TRACES_EXPORTER"], "otlp")
        self.assertEqual(on["env"]["CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"], "1")
        off = otel_setup.apply(on, cfg, helper="/h")
        self.assertNotIn("OTEL_TRACES_EXPORTER", off["env"])
        self.assertNotIn("CLAUDE_CODE_ENHANCED_TELEMETRY_BETA", off["env"])

    def test_reapply_and_disable_round_trip(self):
        settings = {"env": {"FOO": "1", "OTEL_RESOURCE_ATTRIBUTES": "team=ce"}}
        cfg = {"otelEndpoint": "https://a", "otelGcpProject": "p1", "otelInstance": "h"}
        once = otel_setup.apply(settings, cfg, helper="/h")
        twice = otel_setup.apply(once, {**cfg, "otelGcpProject": "p2"}, helper="/h")
        self.assertEqual(twice["env"]["OTEL_RESOURCE_ATTRIBUTES"],
                         "team=ce,gcp.project_id=p2,cloud.region=us-central1,service.instance.id=h")
        self.assertEqual(otel_setup.disable(twice, helper="/h"), settings)


if __name__ == "__main__":
    unittest.main()
