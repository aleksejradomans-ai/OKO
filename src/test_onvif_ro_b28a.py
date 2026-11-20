"""B28a: read-only ONVIF client -- requests, parsing of synthetic responses, probe flow with a fake transport.

All responses here are synthetic (made-up device "ExampleCam"), documentation address 192.0.2.10 (RFC 5737).
"""
import base64
import datetime as dt
import hashlib
import re

import pytest

import onvif_ro as O

HOST = "192.0.2.10"
OTHER = "198.51.100.7"
ENV = ('<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
       'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
       'xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema" '
       'xmlns:ter="http://www.onvif.org/ver10/error"><s:Body>{}</s:Body></s:Envelope>')


def env(body: str) -> bytes:
    return ENV.format(body).encode()


TIME = env("<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime><tt:DateTimeType>NTP</tt:DateTimeType>"
           "<tt:UTCDateTime><tt:Time><tt:Hour>10</tt:Hour><tt:Minute>0</tt:Minute><tt:Second>30</tt:Second></tt:Time>"
           "<tt:Date><tt:Year>2026</tt:Year><tt:Month>11</tt:Month><tt:Day>21</tt:Day></tt:Date></tt:UTCDateTime>"
           "</tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>")
INFO = env("<tds:GetDeviceInformationResponse><tds:Manufacturer>ExampleCam</tds:Manufacturer><tds:Model>Model-T"
           "</tds:Model><tds:FirmwareVersion>V0.0.1</tds:FirmwareVersion><tds:SerialNumber>SN000</tds:SerialNumber>"
           "<tds:HardwareId>HW0</tds:HardwareId></tds:GetDeviceInformationResponse>")
CAPS = env(f"<tds:GetCapabilitiesResponse><tds:Capabilities><tt:Device><tt:XAddr>http://{OTHER}:8899/onvif/device_service"
           f"</tt:XAddr></tt:Device><tt:Media><tt:XAddr>http://{OTHER}:8899/onvif/Media</tt:XAddr></tt:Media>"
           f"<tt:PTZ><tt:XAddr>http://{OTHER}:8899/onvif/PTZ</tt:XAddr></tt:PTZ></tds:Capabilities>"
           "</tds:GetCapabilitiesResponse>")
CAPS_NO_PTZ = env(f"<tds:GetCapabilitiesResponse><tds:Capabilities><tt:Media><tt:XAddr>http://{HOST}:8899/onvif/Media"
                  "</tt:XAddr></tt:Media></tds:Capabilities></tds:GetCapabilitiesResponse>")
SERVICES = env("<tds:GetServicesResponse><tds:Service><tds:Namespace>http://www.onvif.org/ver10/device/wsdl</tds:Namespace>"
               f"<tds:XAddr>http://{HOST}:8899/onvif/device_service</tds:XAddr></tds:Service>"
               "<tds:Service><tds:Namespace>http://www.onvif.org/ver10/media/wsdl</tds:Namespace>"
               f"<tds:XAddr>http://{HOST}:8899/onvif/Media</tds:XAddr></tds:Service>"
               "<tds:Service><tds:Namespace>http://www.onvif.org/ver20/ptz/wsdl</tds:Namespace>"
               f"<tds:XAddr>http://{OTHER}:8899/onvif/PTZ</tds:XAddr></tds:Service></tds:GetServicesResponse>")


def profile(tok, src, w, h, ptz=True):
    p = (f'<trt:Profiles token="{tok}" fixed="true"><tt:Name>{tok}_name</tt:Name>'
         f'<tt:VideoSourceConfiguration token="V{src}"><tt:Name>vsc</tt:Name><tt:SourceToken>{src}</tt:SourceToken>'
         f'<tt:Bounds x="0" y="0" width="{w}" height="{h}"/></tt:VideoSourceConfiguration>'
         f'<tt:VideoEncoderConfiguration token="E{tok}"><tt:Encoding>H264</tt:Encoding><tt:Resolution><tt:Width>{w}'
         f'</tt:Width><tt:Height>{h}</tt:Height></tt:Resolution><tt:RateControl><tt:FrameRateLimit>15</tt:FrameRateLimit>'
         f'</tt:RateControl></tt:VideoEncoderConfiguration>')
    if ptz:
        p += '<tt:PTZConfiguration token="PTZ0"><tt:Name>ptz</tt:Name><tt:NodeToken>N0</tt:NodeToken></tt:PTZConfiguration>'
    return p + "</trt:Profiles>"


PROFILES = env("<trt:GetProfilesResponse>" + profile("P0", "S0", 2304, 1296) + profile("P1", "S1", 2304, 1296)
               + profile("P2", "S0", 640, 360, ptz=False) + "</trt:GetProfilesResponse>")
SOURCES = env('<trt:GetVideoSourcesResponse><trt:VideoSources token="S0"><tt:Framerate>15</tt:Framerate><tt:Resolution>'
              '<tt:Width>2304</tt:Width><tt:Height>1296</tt:Height></tt:Resolution></trt:VideoSources>'
              '<trt:VideoSources token="S1"><tt:Framerate>15</tt:Framerate><tt:Resolution><tt:Width>2304</tt:Width>'
              '<tt:Height>1296</tt:Height></tt:Resolution></trt:VideoSources></trt:GetVideoSourcesResponse>')


def space(kind, uri, x, y=None):
    s = f"<tt:{kind}><tt:URI>{uri}</tt:URI><tt:XRange><tt:Min>{x[0]}</tt:Min><tt:Max>{x[1]}</tt:Max></tt:XRange>"
    if y:
        s += f"<tt:YRange><tt:Min>{y[0]}</tt:Min><tt:Max>{y[1]}</tt:Max></tt:YRange>"
    return s + f"</tt:{kind}>"


NODES = env('<tptz:GetNodesResponse><tptz:PTZNode token="N0" FixedHomePosition="false"><tt:Name>node</tt:Name>'
            "<tt:SupportedPTZSpaces>"
            + space("AbsolutePanTiltPositionSpace", "http://www.onvif.org/ver10/tptz/PanTiltSpaces/PositionGenericSpace", (-1, 1), (-1, 1))
            + space("RelativePanTiltTranslationSpace", "http://www.onvif.org/ver10/tptz/PanTiltSpaces/TranslationGenericSpace", (-1, 1), (-1, 1))
            + space("ContinuousPanTiltVelocitySpace", "http://www.onvif.org/ver10/tptz/PanTiltSpaces/VelocityGenericSpace", (-1, 1), (-1, 1))
            + space("PanTiltSpeedSpace", "http://www.onvif.org/ver10/tptz/PanTiltSpaces/GenericSpeedSpace", (0, 1))
            + "</tt:SupportedPTZSpaces><tt:MaximumNumberOfPresets>255</tt:MaximumNumberOfPresets>"
            "<tt:HomeSupported>true</tt:HomeSupported></tptz:PTZNode></tptz:GetNodesResponse>")
CONFIGS = env('<tptz:GetConfigurationsResponse><tptz:PTZConfiguration token="PTZ0"><tt:Name>ptz</tt:Name>'
              "<tt:NodeToken>N0</tt:NodeToken><tt:DefaultPTZTimeout>PT5S</tt:DefaultPTZTimeout><tt:PanTiltLimits>"
              "<tt:Range><tt:URI>x</tt:URI><tt:XRange><tt:Min>-1</tt:Min><tt:Max>1</tt:Max></tt:XRange><tt:YRange>"
              "<tt:Min>-0.5</tt:Min><tt:Max>1</tt:Max></tt:YRange></tt:Range></tt:PanTiltLimits>"
              "</tptz:PTZConfiguration></tptz:GetConfigurationsResponse>")
PRESETS = env('<tptz:GetPresetsResponse><tptz:Preset token="1"><tt:Name>my desk</tt:Name><tt:PTZPosition>'
              '<tt:PanTilt x="0.25" y="-0.1"/></tt:PTZPosition></tptz:Preset><tptz:Preset token="2"/>'
              "</tptz:GetPresetsResponse>")
STATUS = env('<tptz:GetStatusResponse><tptz:PTZStatus><tt:Position><tt:PanTilt x="0.1" y="0.2"/><tt:Zoom x="0"/>'
             "</tt:Position><tt:MoveStatus><tt:PanTilt>IDLE</tt:PanTilt><tt:Zoom>IDLE</tt:Zoom></tt:MoveStatus>"
             "</tptz:PTZStatus></tptz:GetStatusResponse>")


def soap_fault(sub: str) -> bytes:
    return env(f'<s:Fault><s:Code><s:Value>s:Sender</s:Value><s:Subcode><s:Value>ter:{sub}</s:Value></s:Subcode>'
               f'</s:Code><s:Reason><s:Text xml:lang="en">x</s:Text></s:Reason></s:Fault>')


ANSWERS = {"GetSystemDateAndTime": TIME, "GetDeviceInformation": INFO, "GetCapabilities": CAPS,
           "GetServices": SERVICES, "GetProfiles": PROFILES, "GetVideoSources": SOURCES, "GetNodes": NODES,
           "GetConfigurations": CONFIGS, "GetPresets": PRESETS, "GetStatus": STATUS}


class Fake:
    """Transport: answers by the operation in the body; records every request."""

    def __init__(self, answers=None, port_ok=8899, status=None):
        self.answers = dict(ANSWERS if answers is None else answers)
        self.port_ok, self.status, self.sent = port_ok, status or {}, []

    def __call__(self, url, data, timeout):
        self.sent.append((url, data.decode()))
        if f":{self.port_ok}/" not in url:
            raise ConnectionRefusedError("refused")
        op = re.search(r"<s:Body><\w+:(\w+)", data.decode()).group(1)
        if op not in self.answers:
            return O.Reply(500, soap_fault("ActionNotSupported"))
        return O.Reply(self.status.get(op, 200), self.answers[op])


def clock():
    t = [0.0]

    def f():
        t[0] += 0.05
        return t[0]
    return f


NOW = lambda: dt.datetime(2026, 11, 21, 10, 0, 0, tzinfo=dt.timezone.utc)  # noqa: E731


def run(fake, **kw):
    return O.probe(HOST, "viewer", "pw-123", ports=kw.pop("ports", (80, 8899)), transport=fake, clock=clock(), now=NOW, **kw)


# ------------------------------------------------------------------ read-only guard
@pytest.mark.parametrize("op", ["AbsoluteMove", "RelativeMove", "ContinuousMove", "Stop", "GotoPreset", "SetPreset",
                                "RemovePreset", "GotoHomePosition", "SetHomePosition", "SystemReboot", "SetSystemDateAndTime",
                                "CreateProfile", "getStatus", "GetStatusX", ""])
def test_non_read_only_operations_are_refused(op):
    with pytest.raises(O.NotReadOnly):
        O.envelope(op, "u", "p")


def test_read_only_list_is_get_only():
    assert O.READ_ONLY_OPS and all(op.startswith("Get") for op in O.READ_ONLY_OPS)
    assert set(O.READ_ONLY_OPS.values()) <= set(O.NS)


def test_client_refuses_other_host_and_write_op():
    cl = O.Client(HOST, "u", "p", transport=Fake())
    with pytest.raises(ValueError):
        cl.call(f"http://{OTHER}:8899/onvif/PTZ", "GetStatus", "P0")
    with pytest.raises(O.NotReadOnly):
        cl.call(f"http://{HOST}:8899/onvif/PTZ", "AbsoluteMove")


def test_probe_sends_only_get_operations_to_the_configured_host():
    fake = Fake()
    run(fake)
    assert fake.sent
    for url, body in fake.sent:
        assert url.startswith(f"http://{HOST}:")
        assert OTHER not in url
        ops = re.findall(r"<s:Body><\w+:(\w+)", body)
        assert len(ops) == 1 and ops[0] in O.READ_ONLY_OPS


# ------------------------------------------------------------------ envelope / digest
def test_password_digest_vector():
    nonce = bytes(range(16))
    created = "2026-11-21T10:00:30Z"
    want = base64.b64encode(hashlib.sha1(nonce + created.encode() + b"pw-123").digest()).decode()
    assert O.digest(nonce, created, "pw-123") == want
    e = O.envelope("GetDeviceInformation", "viewer", "pw-123", nonce=nonce, created=created)
    assert want in e and base64.b64encode(nonce).decode() in e and created in e
    assert "pw-123" not in e


def test_no_auth_header_for_system_time_and_escaping():
    e = O.envelope("GetSystemDateAndTime", "viewer", "pw")
    assert "Security" not in e
    e = O.envelope("GetStatus", "a<b&c", "pw", profile_token='P"0')
    assert "a&lt;b&amp;c" in e and "P&quot;0" in e
    with pytest.raises(ValueError):
        O.envelope("GetPresets", "u", "p")             # needs a profile token


def test_digest_uses_camera_clock_offset():
    fake = Fake()
    res = run(fake)
    assert res.clock_offset_s == 30.0
    body = next(b for u, b in fake.sent if "GetDeviceInformation" in b)
    assert "<wsu:Created>2026-11-21T10:00:30Z</wsu:Created>" in body


# ------------------------------------------------------------------ parsers
def test_pin_host_keeps_path_and_port_only():
    assert O.pin_host(f"http://{OTHER}:8899/onvif/PTZ", HOST, 80) == f"http://{HOST}:8899/onvif/PTZ"
    assert O.pin_host(f"http://{OTHER}/onvif/Media", HOST, 8000) == f"http://{HOST}:8000/onvif/Media"
    assert O.pin_host("garbage", HOST, 80).startswith(f"http://{HOST}:80/")


def test_parse_profiles_and_sources_per_lens():
    pr = O.parse_profiles(O.parse_xml(PROFILES))
    assert [p["token"] for p in pr] == ["P0", "P1", "P2"]
    assert pr[0]["name"] == "P0_name" and pr[0]["source_token"] == "S0" and pr[1]["source_token"] == "S1"
    assert pr[0]["resolution"] == [2304, 1296] and pr[2]["resolution"] == [640, 360]
    assert pr[0]["bounds"] == [0, 0, 2304, 1296] and pr[0]["fps"] == 15.0
    assert pr[0]["ptz_config"] == "PTZ0" and pr[2]["ptz_config"] == ""
    vs = O.parse_video_sources(O.parse_xml(SOURCES))
    assert [v["token"] for v in vs] == ["S0", "S1"] and vs[1]["resolution"] == [2304, 1296]


def test_parse_nodes_spaces_and_moves():
    n = O.parse_nodes(O.parse_xml(NODES))
    assert len(n) == 1 and n[0]["token"] == "N0" and n[0]["max_presets"] == 255 and n[0]["home_supported"]
    sp = n[0]["spaces"]
    assert sp["AbsolutePanTiltPositionSpace"][0]["x"] == [-1.0, 1.0] and sp["AbsolutePanTiltPositionSpace"][0]["y"] == [-1.0, 1.0]
    assert sp["PanTiltSpeedSpace"][0]["x"] == [0.0, 1.0] and sp["PanTiltSpeedSpace"][0]["y"] is None
    m = O.moves_supported(n[0])
    assert m == {"absolute_pt": True, "absolute_zoom": False, "relative_pt": True, "relative_zoom": False,
                 "continuous_pt": True, "continuous_zoom": False, "home": True}


def test_parse_configurations_presets_status():
    c = O.parse_configurations(O.parse_xml(CONFIGS))
    assert c[0]["node_token"] == "N0" and c[0]["pan_limits"] == [-1.0, 1.0] and c[0]["tilt_limits"] == [-0.5, 1.0]
    assert c[0]["zoom_limits"] is None and c[0]["default_timeout"] == "PT5S"
    p = O.parse_presets(O.parse_xml(PRESETS))
    assert p == [{"token": "1", "has_name": True, "pan": 0.25, "tilt": -0.1, "zoom": None},
                 {"token": "2", "has_name": False, "pan": None, "tilt": None, "zoom": None}]
    s = O.parse_status(O.parse_xml(STATUS))
    assert s == {"pan": 0.1, "tilt": 0.2, "zoom": 0.0, "move_pt": "IDLE", "move_zoom": "IDLE", "error": ""}


def test_parse_capabilities_services_and_lookup():
    caps = O.parse_capabilities(O.parse_xml(CAPS))
    assert set(caps) == {"Device", "Media", "PTZ"}
    sv = O.parse_services(O.parse_xml(SERVICES))
    assert O.service_addr(caps, sv, "ptz") == f"http://{OTHER}:8899/onvif/PTZ"
    assert O.service_addr(O.parse_capabilities(O.parse_xml(CAPS_NO_PTZ)), {}, "ptz") == ""


def test_fault_codes_and_classification():
    assert O.fault(O.parse_xml(soap_fault("ActionNotSupported"))) == "ActionNotSupported"
    assert O.classify(O.Reply(400, soap_fault("ActionNotSupported")), None)[:2] == ("NOT_SUPPORTED", "ActionNotSupported")
    assert O.classify(O.Reply(400, soap_fault("NotAuthorized")), None)[:2] == ("ERROR", "NotAuthorized")
    assert O.classify(O.Reply(404, b"<html>nope"), None)[:2] == ("NOT_SUPPORTED", "HTTP_404")
    assert O.classify(O.Reply(401, b""), None)[:2] == ("ERROR", "HTTP_401_NOT_XML")
    assert O.classify(O.Reply(500, STATUS), None)[:2] == ("ERROR", "HTTP_500")
    assert O.classify(None, TimeoutError("t"))[:2] == ("ERROR", "TIMEOUT")
    assert O.classify(None, ConnectionRefusedError("r"))[:2] == ("ERROR", "CONNECT")
    assert O.classify(O.Reply(200, STATUS), None)[:2] == ("OK", "")


def test_dtd_and_large_responses_rejected():
    evil = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>'
    with pytest.raises(O.ResponseRejected):
        O.parse_xml(evil)
    assert O.classify(O.Reply(200, evil), None)[:2] == ("ERROR", "DTD_REJECTED")
    big = b"<x>" + b"a" * (O.MAX_RESPONSE + 10) + b"</x>"
    assert O.classify(O.Reply(200, big), None)[:2] == ("ERROR", "TOO_LARGE")


# ------------------------------------------------------------------ probe flow
def test_probe_full_flow():
    res = run(Fake())
    st = {s.op: s.status for s in res.steps}
    assert res.port == 8899
    assert all(v == "OK" for v in st.values()), st
    assert set(st) == set(O.READ_ONLY_OPS)
    assert res.public["ports_tried"][0].startswith("80:ERROR/CONNECT")
    assert len(res.public["profiles"]) == 3 and len(res.public["video_sources"]) == 2
    assert res.public["moves"][0]["absolute_pt"] and res.public["presets"]["count"] == 2
    assert res.public["status"]["pan"] == 0.1 and res.ptz_profile == "P0"
    assert res.ptz_url == f"http://{HOST}:8899/onvif/PTZ"
    assert all(s.ms is not None and s.ms >= 0 for s in res.steps)


def test_identity_only_in_private_part():
    res = run(Fake())
    assert res.private["device"]["Model"] == "Model-T"
    pub = repr(res.public) + "".join(s.line() for s in res.steps)
    for secret in ("ExampleCam", "Model-T", "V0.0.1", "SN000", "HW0", HOST, OTHER, "my desk", "pw-123", "viewer"):
        assert secret not in pub


def test_no_ptz_service_gives_no_data():
    a = dict(ANSWERS, GetCapabilities=CAPS_NO_PTZ)
    a.pop("GetServices")
    res = run(Fake(a))
    st = {s.op: (s.status, s.code) for s in res.steps}
    assert st["GetServices"][0] == "NOT_SUPPORTED"
    for op in ("GetNodes", "GetConfigurations", "GetPresets", "GetStatus"):
        assert st[op] == ("NO_DATA", "NO_PTZ_SERVICE")
    assert res.public["ptz_service"] is False and res.public["media_service"] is True


def test_no_onvif_answer_gives_no_data_everywhere():
    res = run(Fake(port_ok=1))
    assert res.port is None
    assert {s.status for s in res.steps} == {"NO_DATA"}
    assert res.public["ports_tried"] == ["80:ERROR/CONNECT", "8899:ERROR/CONNECT"]
    assert O.probe("", "u", "p").steps[0].code == "NO_HOST"


def test_presets_unsupported_is_reported_not_fatal():
    a = dict(ANSWERS)
    a.pop("GetPresets")
    res = run(Fake(a))
    st = {s.op: s.status for s in res.steps}
    assert st["GetPresets"] == "NOT_SUPPORTED" and st["GetStatus"] == "OK"
    assert res.public["presets"]["count"] == 0


def test_status_again_and_same_position():
    fake = Fake()
    res = run(fake)
    st, pos = O.status_again(HOST, "viewer", "pw-123", res, transport=fake, clock=clock(), offset_s=30)
    assert st.status == "OK" and O.same_position(res.public["status"], pos) is True
    assert O.same_position({"pan": 0.1, "tilt": 0.2, "zoom": 0.0}, {"pan": 0.15, "tilt": 0.2, "zoom": 0.0}) is False
    assert O.same_position({}, pos) is None
    assert O.status_again(HOST, "u", "p", O.ProbeResult())[0].code == "NO_PTZ_PROFILE"


def test_mask_replaces_host_user_password():
    s = f"rtsp://viewer:pw-123@{HOST}:554/x user=viewer"
    m = O.mask(s, HOST, "viewer", "pw-123")
    assert HOST not in m and "viewer" not in m and "pw-123" not in m
    assert m == "rtsp://<user>:***@<cam>:554/x user=<user>"
