"""ONVIF_RO: read-only ONVIF client for the PTZ camera -- capability probe without any movement.

Why: before the camera is ever turned by a command, the project has to know what the camera really supports
(profiles per lens, PTZ spaces and ranges, presets, home position, current position). This module only ASKS.

Rules (zero-trust, fail-closed):
  * only operations from READ_ONLY_OPS (all of them Get*) can be built into a request; anything else raises
    NotReadOnly before a single byte is sent (Move / Stop / Set* / Goto* / Create* / Remove* / Reboot ...);
  * only the configured camera host is contacted: a service address (XAddr) reported by the camera is reduced to its
    path and port, the host is always the configured one -- no discovery, no broadcast, no other address;
  * a response is parsed only if it is small (MAX_RESPONSE bytes) and has no DTD / entity declarations;
  * the password is used only inside the WS-Security digest; it is never logged and never returned. Device identity
    (manufacturer, model, firmware, serial) is kept in a separate "private" part of the result -- it must not go into
    public files.
Raw SOAP over the HTTP client already used by the project (httpx); no ONVIF library.

Each probe step gives a status: OK / NOT_SUPPORTED / ERROR (with a code) / NO_DATA (precondition missing),
plus the response time in ms.
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urlsplit

NS = {
    "device": "http://www.onvif.org/ver10/device/wsdl",
    "media": "http://www.onvif.org/ver10/media/wsdl",
    "ptz": "http://www.onvif.org/ver20/ptz/wsdl",
}
SOAP_ENV = "http://www.w3.org/2003/05/soap-envelope"
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
PW_DIGEST = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
NONCE_B64 = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary"

# operation -> service; the ONLY operations this module can send. All read-only.
READ_ONLY_OPS = {
    "GetSystemDateAndTime": "device",
    "GetDeviceInformation": "device",
    "GetCapabilities": "device",
    "GetServices": "device",
    "GetProfiles": "media",
    "GetVideoSources": "media",
    "GetNodes": "ptz",
    "GetConfigurations": "ptz",
    "GetPresets": "ptz",
    "GetStatus": "ptz",
}
NO_AUTH_OPS = {"GetSystemDateAndTime"}          # the camera clock is needed before the digest can be built
DEFAULT_PORTS = (80, 8899, 8000, 8080)          # tried in this order on the configured host only (OKO_CAM_ONVIF_PORT first)
DEVICE_PATH = "/onvif/device_service"
MAX_RESPONSE = 1_000_000
TIMEOUT_S = 4.0
NOT_SUPPORTED_CODES = ("ActionNotSupported", "NoSuchService", "NotSupported", "NoPTZProfile", "NoProfile",
                       "InvalidOperation")


class NotReadOnly(ValueError):
    """An operation outside READ_ONLY_OPS was requested."""


class ResponseRejected(ValueError):
    """The response is too large, has a DTD / entity declaration, or is not XML."""


# ---------------------------------------------------------------------------------------------------- requests
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def digest(nonce: bytes, created: str, password: str) -> str:
    """WS-Security UsernameToken PasswordDigest = Base64(SHA1(nonce + created + password))."""
    return base64.b64encode(hashlib.sha1(nonce + created.encode("utf-8") + password.encode("utf-8")).digest()).decode()


def _xml_text(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def check_read_only(op: str) -> str:
    """Service of a read-only operation; anything else -> NotReadOnly (nothing is built, nothing is sent)."""
    if op not in READ_ONLY_OPS or not op.startswith("Get"):
        raise NotReadOnly(f"operation {op!r} is not in the read-only list")
    return READ_ONLY_OPS[op]


def body_for(op: str, profile_token: str = "") -> str:
    svc = check_read_only(op)
    ns = NS[svc]
    if op == "GetCapabilities":
        inner = f'<tds:Category>All</tds:Category>'
        return f'<tds:GetCapabilities xmlns:tds="{ns}">{inner}</tds:GetCapabilities>'
    if op == "GetServices":
        return f'<tds:GetServices xmlns:tds="{ns}"><tds:IncludeCapability>false</tds:IncludeCapability></tds:GetServices>'
    if op in ("GetPresets", "GetStatus"):
        if not profile_token:
            raise ValueError(f"{op} needs a profile token")
        return f'<tptz:{op} xmlns:tptz="{ns}"><tptz:ProfileToken>{_xml_text(profile_token)}</tptz:ProfileToken></tptz:{op}>'
    prefix = {"device": "tds", "media": "trt", "ptz": "tptz"}[svc]
    return f'<{prefix}:{op} xmlns:{prefix}="{ns}"/>'


def envelope(op: str, user: str = "", password: str = "", nonce: Optional[bytes] = None,
             created: Optional[str] = None, profile_token: str = "") -> str:
    """SOAP 1.2 envelope of a read-only operation; WS-Security header unless the op needs no auth or no user."""
    body = body_for(op, profile_token)
    header = ""
    if user and op not in NO_AUTH_OPS:
        nonce = nonce if nonce is not None else os.urandom(16)
        created = created or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        header = (f'<s:Header><wsse:Security xmlns:wsse="{WSSE}" xmlns:wsu="{WSU}" s:mustUnderstand="1">'
                  f'<wsse:UsernameToken><wsse:Username>{_xml_text(user)}</wsse:Username>'
                  f'<wsse:Password Type="{PW_DIGEST}">{digest(nonce, created, password)}</wsse:Password>'
                  f'<wsse:Nonce EncodingType="{NONCE_B64}">{base64.b64encode(nonce).decode()}</wsse:Nonce>'
                  f'<wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security></s:Header>')
    return f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="{SOAP_ENV}">{header}<s:Body>{body}</s:Body></s:Envelope>'


def pin_host(xaddr: str, host: str, default_port: int) -> str:
    """Service address on the CONFIGURED host: path and port from the camera's XAddr, host never taken from it."""
    try:
        u = urlsplit(xaddr.strip())
        port = u.port or default_port
        path = u.path or DEVICE_PATH
    except ValueError:
        port, path = default_port, DEVICE_PATH
    if not path.startswith("/"):
        path = "/" + path
    return f"http://{host}:{port}{path}"


# ---------------------------------------------------------------------------------------------------- responses
_DTD = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.I)


def parse_xml(raw: bytes) -> ET.Element:
    if len(raw) > MAX_RESPONSE:
        raise ResponseRejected("response too large")
    if _DTD.search(raw):
        raise ResponseRejected("DTD / entity declaration in the response")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as e:
        raise ResponseRejected(f"not XML ({e.__class__.__name__})") from None


def find(el: Optional[ET.Element], *names: str) -> Optional[ET.Element]:
    """First descendant along a chain of local names (namespace-agnostic)."""
    cur = el
    for n in names:
        if cur is None:
            return None
        cur = next((c for c in cur.iter() if c is not cur and _local(c.tag) == n), None)
    return cur


def find_all(el: Optional[ET.Element], name: str, direct: bool = False) -> list[ET.Element]:
    if el is None:
        return []
    it = list(el) if direct else [c for c in el.iter() if c is not el]
    return [c for c in it if _local(c.tag) == name]


def text(el: Optional[ET.Element], *names: str, default: str = "") -> str:
    t = find(el, *names) if names else el
    return (t.text or "").strip() if t is not None and t.text is not None else default


def num(s: str) -> Optional[float]:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def fault(root: ET.Element) -> Optional[str]:
    """SOAP fault -> most specific code (local name of the deepest Subcode value, else Code value); None if no fault."""
    f = find(root, "Fault")
    if f is None:
        return None
    vals = [_local_value(v.text) for v in f.iter() if _local(v.tag) == "Value" and v.text]
    if not vals:
        fc = text(f, "faultcode")
        return _local_value(fc) or "Fault"
    return vals[-1]


def _local_value(s: Optional[str]) -> str:
    s = (s or "").strip()
    return s.rsplit(":", 1)[-1] if s else ""


def parse_system_time(root: ET.Element) -> Optional[_dt.datetime]:
    u = find(root, "UTCDateTime")
    if u is None:
        return None
    try:
        d, t = find(u, "Date"), find(u, "Time")
        return _dt.datetime(int(text(d, "Year")), int(text(d, "Month")), int(text(d, "Day")),
                            int(text(t, "Hour")), int(text(t, "Minute")), int(text(t, "Second")),
                            tzinfo=_dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def parse_device_info(root: ET.Element) -> dict:
    """PRIVATE: identity of the device. Never into public files."""
    r = find(root, "GetDeviceInformationResponse")
    keys = ("Manufacturer", "Model", "FirmwareVersion", "SerialNumber", "HardwareId")
    return {k: text(r, k) for k in keys} if r is not None else {}


def parse_capabilities(root: ET.Element) -> dict:
    """Capability -> XAddr for the categories present (Device, Media, PTZ, Imaging, Events, Analytics ...)."""
    caps = find(root, "Capabilities")
    res = {}
    for c in (list(caps) if caps is not None else []):
        xa = text(c, "XAddr")
        if xa:
            res[_local(c.tag)] = xa
    return res


def parse_services(root: ET.Element) -> dict:
    """Namespace -> XAddr; service kinds by namespace suffix (device / media / ptz / media2 / imaging ...)."""
    res = {}
    for s in find_all(root, "Service"):
        ns, xa = text(s, "Namespace"), text(s, "XAddr")
        if ns and xa:
            res[ns] = xa
    return res


def service_addr(caps: dict, services: dict, kind: str) -> str:
    """XAddr of a service: GetServices namespace first, GetCapabilities category second; '' if absent."""
    want = {"media": NS["media"], "ptz": NS["ptz"], "device": NS["device"]}[kind]
    if want in services:
        return services[want]
    cat = {"media": "Media", "ptz": "PTZ", "device": "Device"}[kind]
    return caps.get(cat, "")


def _res(el: Optional[ET.Element]) -> Optional[list]:
    if el is None:
        return None
    w, h = num(text(el, "Width")), num(text(el, "Height"))
    return [int(w), int(h)] if w is not None and h is not None else None


def parse_profiles(root: ET.Element) -> list[dict]:
    out = []
    for p in find_all(root, "Profiles"):
        vsc = find(p, "VideoSourceConfiguration")
        vec = find(p, "VideoEncoderConfiguration")
        ptz = find(p, "PTZConfiguration")
        bounds = find(vsc, "Bounds") if vsc is not None else None
        out.append({
            "token": p.get("token", ""),
            "name": next((text(c) for c in find_all(p, "Name", direct=True)), ""),
            "source_token": text(vsc, "SourceToken") if vsc is not None else "",
            "bounds": [int(num(bounds.get(k, "0")) or 0) for k in ("x", "y", "width", "height")] if bounds is not None else None,
            "encoding": text(vec, "Encoding") if vec is not None else "",
            "resolution": _res(find(vec, "Resolution")) if vec is not None else None,
            "fps": num(text(vec, "FrameRateLimit")) if vec is not None else None,
            "ptz_config": ptz.get("token", "") if ptz is not None else "",
        })
    return out


def parse_video_sources(root: ET.Element) -> list[dict]:
    out = []
    for v in find_all(root, "VideoSources"):
        out.append({"token": v.get("token", ""), "framerate": num(text(v, "Framerate")),
                    "resolution": _res(find(v, "Resolution"))})
    return out


def _range(el: Optional[ET.Element], name: str) -> Optional[list]:
    r = find(el, name) if el is not None else None
    if r is None:
        return None
    lo, hi = num(text(r, "Min")), num(text(r, "Max"))
    return [lo, hi] if lo is not None and hi is not None else None


SPACE_KINDS = ("AbsolutePanTiltPositionSpace", "AbsoluteZoomPositionSpace", "RelativePanTiltTranslationSpace",
               "RelativeZoomTranslationSpace", "ContinuousPanTiltVelocitySpace", "ContinuousZoomVelocitySpace",
               "PanTiltSpeedSpace", "ZoomSpeedSpace")


def parse_nodes(root: ET.Element) -> list[dict]:
    out = []
    for n in find_all(root, "PTZNode"):
        sp = find(n, "SupportedPTZSpaces")
        spaces = {}
        for kind in SPACE_KINDS:
            items = find_all(sp, kind, direct=True) if sp is not None else []
            spaces[kind] = [{"uri": text(i, "URI"), "x": _range(i, "XRange"), "y": _range(i, "YRange")} for i in items]
        out.append({
            "token": n.get("token", ""),
            "fixed_home": n.get("FixedHomePosition", ""),
            "spaces": spaces,
            "max_presets": int(num(text(n, "MaximumNumberOfPresets")) or 0),
            "home_supported": text(n, "HomeSupported").lower() == "true",
            "aux": [a.text.strip() for a in find_all(n, "AuxiliaryCommands") if a.text],
        })
    return out


def moves_supported(node: dict) -> dict:
    """Which kinds of movement a node declares (pan/tilt and zoom separately)."""
    s = node.get("spaces", {})
    return {
        "absolute_pt": bool(s.get("AbsolutePanTiltPositionSpace")),
        "absolute_zoom": bool(s.get("AbsoluteZoomPositionSpace")),
        "relative_pt": bool(s.get("RelativePanTiltTranslationSpace")),
        "relative_zoom": bool(s.get("RelativeZoomTranslationSpace")),
        "continuous_pt": bool(s.get("ContinuousPanTiltVelocitySpace")),
        "continuous_zoom": bool(s.get("ContinuousZoomVelocitySpace")),
        "home": bool(node.get("home_supported")),
    }


def parse_configurations(root: ET.Element) -> list[dict]:
    out = []
    for c in find_all(root, "PTZConfiguration"):
        ptl = find(c, "PanTiltLimits")
        zl = find(c, "ZoomLimits")
        out.append({
            "token": c.get("token", ""),
            "node_token": text(c, "NodeToken"),
            "default_timeout": text(c, "DefaultPTZTimeout"),
            "pan_limits": _range(ptl, "XRange") if ptl is not None else None,
            "tilt_limits": _range(ptl, "YRange") if ptl is not None else None,
            "zoom_limits": _range(zl, "XRange") if zl is not None else None,
        })
    return out


def _pos(el: Optional[ET.Element]) -> dict:
    pt, z = find(el, "PanTilt"), find(el, "Zoom")
    return {"pan": num(pt.get("x")) if pt is not None else None, "tilt": num(pt.get("y")) if pt is not None else None,
            "zoom": num(z.get("x")) if z is not None else None}


def parse_presets(root: ET.Element) -> list[dict]:
    """Tokens and positions of presets; names are kept out (they may be personal labels)."""
    out = []
    for p in find_all(root, "Preset"):
        out.append({"token": p.get("token", ""), "has_name": bool(text(p, "Name")), **_pos(find(p, "PTZPosition"))})
    return out


def parse_status(root: ET.Element) -> dict:
    st = find(root, "PTZStatus")
    ms = find(st, "MoveStatus") if st is not None else None
    return {**_pos(find(st, "Position") if st is not None else None),
            "move_pt": text(ms, "PanTilt") if ms is not None else "",
            "move_zoom": text(ms, "Zoom") if ms is not None else "",
            "error": text(st, "Error") if st is not None else ""}


# ---------------------------------------------------------------------------------------------------- transport
@dataclass
class Reply:
    status: int
    body: bytes


Transport = Callable[[str, bytes, float], Reply]      # (url, request body, timeout) -> Reply; raises on network error


def httpx_transport(url: str, data: bytes, timeout: float) -> Reply:
    import httpx
    hdr = {"Content-Type": 'application/soap+xml; charset=utf-8'}
    with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as c:
        r = c.post(url, content=data, headers=hdr)
        return Reply(r.status_code, r.content[:MAX_RESPONSE + 1])


@dataclass
class Step:
    op: str
    status: str = "NO_DATA"           # OK / NOT_SUPPORTED / ERROR / NO_DATA
    code: str = ""
    ms: Optional[int] = None
    http: Optional[int] = None

    def line(self) -> str:
        t = f" ms={self.ms}" if self.ms is not None else ""
        h = f" http={self.http}" if self.http is not None else ""
        c = f" code={self.code}" if self.code else ""
        return f"{self.op} {self.status}{c}{h}{t}"


@dataclass
class ProbeResult:
    port: Optional[int] = None
    clock_offset_s: Optional[float] = None
    steps: list = field(default_factory=list)
    public: dict = field(default_factory=dict)     # capabilities, profiles, ptz -- no identity, no host
    private: dict = field(default_factory=dict)    # device identity -- private report only
    raw: dict = field(default_factory=dict)        # op -> raw response bytes (private storage only)
    ptz_url: str = ""                              # pinned to the configured host; not for reports
    ptz_profile: str = ""

    def step(self, op: str) -> Optional[Step]:
        return next((s for s in self.steps if s.op == op), None)


def classify(reply: Optional[Reply], exc: Optional[BaseException]) -> tuple[str, str, Optional[ET.Element]]:
    """(status, code, parsed root) of one exchange."""
    if exc is not None:
        name = exc.__class__.__name__
        if "Timeout" in name:
            return "ERROR", "TIMEOUT", None
        if "Connect" in name or isinstance(exc, (ConnectionError, OSError)):
            return "ERROR", "CONNECT", None
        return "ERROR", name, None
    assert reply is not None
    try:
        root = parse_xml(reply.body)
    except ResponseRejected as e:
        if reply.status in (404, 405):
            return "NOT_SUPPORTED", f"HTTP_{reply.status}", None
        code = "DTD_REJECTED" if "DTD" in str(e) else ("TOO_LARGE" if "large" in str(e) else f"HTTP_{reply.status}_NOT_XML")
        return "ERROR", code, None
    fc = fault(root)
    if fc:
        if any(k.lower() in fc.lower() for k in NOT_SUPPORTED_CODES):
            return "NOT_SUPPORTED", fc, root
        return "ERROR", fc, root
    if reply.status != 200:
        return ("NOT_SUPPORTED" if reply.status in (404, 405) else "ERROR"), f"HTTP_{reply.status}", root
    return "OK", "", root


class Client:
    """Read-only ONVIF client bound to ONE host."""

    def __init__(self, host: str, user: str, password: str, transport: Transport = httpx_transport,
                 clock: Callable[[], float] = time.monotonic, timeout: float = TIMEOUT_S,
                 now: Callable[[], _dt.datetime] = lambda: _dt.datetime.now(_dt.timezone.utc)):
        self.host, self.user, self._pw = host, user, password
        self.transport, self.clock, self.timeout, self.now = transport, clock, timeout, now
        self.offset = _dt.timedelta(0)

    def created(self) -> str:
        return (self.now() + self.offset).strftime("%Y-%m-%dT%H:%M:%SZ")

    def call(self, url: str, op: str, profile_token: str = "") -> tuple[Step, Optional[ET.Element], bytes]:
        check_read_only(op)
        if urlsplit(url).hostname != self.host:
            raise ValueError("refusing to contact a host other than the configured camera")
        data = envelope(op, self.user, self._pw, created=self.created(), profile_token=profile_token).encode("utf-8")
        st = Step(op)
        t0 = self.clock()
        reply, exc = None, None
        try:
            reply = self.transport(url, data, self.timeout)
        except Exception as e:          # network errors become a status, never a crash of the probe
            exc = e
        st.ms = int(round((self.clock() - t0) * 1000))
        st.http = reply.status if reply is not None else None
        st.status, st.code, root = classify(reply, exc)
        return st, root, (reply.body if reply is not None else b"")


def probe(host: str, user: str, password: str, ports=None, transport: Transport = httpx_transport,
          clock: Callable[[], float] = time.monotonic,
          now: Callable[[], _dt.datetime] = lambda: _dt.datetime.now(_dt.timezone.utc),
          timeout: float = TIMEOUT_S) -> ProbeResult:
    """All read-only steps of the capability probe; never raises for camera-side problems."""
    res = ProbeResult()
    if not host:
        res.steps.append(Step("GetSystemDateAndTime", "NO_DATA", "NO_HOST"))
        return res
    cl = Client(host, user, password, transport, clock, timeout, now)
    ports = list(ports) if ports else list(DEFAULT_PORTS)

    def keep(st: Step, raw: bytes):
        res.steps.append(st)
        if raw:
            res.raw[st.op] = raw

    # 1. device service: the first port whose GetSystemDateAndTime answers with SOAP
    dev = ""
    tried = []
    for p in ports:
        url = f"http://{host}:{p}{DEVICE_PATH}"
        st, root, raw = cl.call(url, "GetSystemDateAndTime")
        tried.append(f"{p}:{st.status}{('/' + st.code) if st.code else ''}")
        if root is not None:                       # any SOAP answer (even a fault) = an ONVIF device service
            res.port, dev = p, url
            keep(st, raw)
            t = parse_system_time(root) if st.status == "OK" else None
            if t is not None:
                cl.offset = t - now()
                res.clock_offset_s = round(cl.offset.total_seconds(), 1)
            break
    res.public["ports_tried"] = tried
    if not dev:
        res.steps.append(Step("GetSystemDateAndTime", "NO_DATA", "NO_ONVIF_ANSWER"))
        for op in ("GetDeviceInformation", "GetCapabilities", "GetServices", "GetProfiles", "GetVideoSources",
                   "GetNodes", "GetConfigurations", "GetPresets", "GetStatus"):
            res.steps.append(Step(op, "NO_DATA", "NO_DEVICE_SERVICE"))
        return res

    st, root, raw = cl.call(dev, "GetDeviceInformation")
    keep(st, raw)
    if st.status == "OK":
        res.private["device"] = parse_device_info(root)

    st, root, raw = cl.call(dev, "GetCapabilities")
    keep(st, raw)
    caps = parse_capabilities(root) if st.status == "OK" else {}
    st, root, raw = cl.call(dev, "GetServices")
    keep(st, raw)
    services = parse_services(root) if st.status == "OK" else {}
    res.public["capabilities"] = sorted(caps)
    res.public["services"] = sorted(ns.rsplit("/", 2)[-2] if ns.endswith("/wsdl") else ns for ns in services)

    media = service_addr(caps, services, "media")
    ptz = service_addr(caps, services, "ptz")
    media_url = pin_host(media, host, res.port) if media else ""
    ptz_url = pin_host(ptz, host, res.port) if ptz else ""
    res.public["media_service"] = bool(media_url)
    res.public["ptz_service"] = bool(ptz_url)

    profiles: list = []
    if media_url:
        st, root, raw = cl.call(media_url, "GetProfiles")
        keep(st, raw)
        profiles = parse_profiles(root) if st.status == "OK" else []
        st, root, raw = cl.call(media_url, "GetVideoSources")
        keep(st, raw)
        res.public["video_sources"] = parse_video_sources(root) if st.status == "OK" else []
    else:
        res.steps += [Step("GetProfiles", "NO_DATA", "NO_MEDIA_SERVICE"), Step("GetVideoSources", "NO_DATA", "NO_MEDIA_SERVICE")]
    res.public["profiles"] = profiles

    ptz_profile = next((p["token"] for p in profiles if p.get("ptz_config")), "")
    res.ptz_url, res.ptz_profile = ptz_url, ptz_profile
    if not ptz_url:
        for op in ("GetNodes", "GetConfigurations", "GetPresets", "GetStatus"):
            res.steps.append(Step(op, "NO_DATA", "NO_PTZ_SERVICE"))
        return res
    st, root, raw = cl.call(ptz_url, "GetNodes")
    keep(st, raw)
    nodes = parse_nodes(root) if st.status == "OK" else []
    res.public["nodes"] = nodes
    res.public["moves"] = [moves_supported(n) for n in nodes]
    st, root, raw = cl.call(ptz_url, "GetConfigurations")
    keep(st, raw)
    res.public["ptz_configurations"] = parse_configurations(root) if st.status == "OK" else []
    if not ptz_profile:
        res.steps += [Step("GetPresets", "NO_DATA", "NO_PTZ_PROFILE"), Step("GetStatus", "NO_DATA", "NO_PTZ_PROFILE")]
        return res
    st, root, raw = cl.call(ptz_url, "GetPresets", ptz_profile)
    keep(st, raw)
    pr = parse_presets(root) if st.status == "OK" else []
    res.public["presets"] = {"count": len(pr), "positions": [{k: p[k] for k in ("pan", "tilt", "zoom")} for p in pr]}
    st, root, raw = cl.call(ptz_url, "GetStatus", ptz_profile)
    keep(st, raw)
    res.public["status"] = parse_status(root) if st.status == "OK" else {}
    return res


def status_again(host: str, user: str, password: str, res: ProbeResult, transport: Transport = httpx_transport,
                 clock: Callable[[], float] = time.monotonic, offset_s: float = 0.0) -> tuple[Step, dict]:
    """One more GetStatus at the end of the probe: the position must be the same -- proof that nothing moved."""
    if not (res.ptz_url and res.ptz_profile):
        return Step("GetStatus", "NO_DATA", "NO_PTZ_PROFILE"), {}
    cl = Client(host, user, password, transport, clock)
    cl.offset = _dt.timedelta(seconds=offset_s)
    st, root, _ = cl.call(res.ptz_url, "GetStatus", res.ptz_profile)
    return st, (parse_status(root) if st.status == "OK" else {})


def same_position(a: dict, b: dict, tol: float = 1e-3) -> Optional[bool]:
    """True / False when both positions are known; None when either is missing."""
    keys = ("pan", "tilt", "zoom")
    if not a or not b or all(a.get(k) is None for k in keys) or all(b.get(k) is None for k in keys):
        return None
    for k in keys:
        x, y = a.get(k), b.get(k)
        if (x is None) != (y is None) or (x is not None and abs(x - y) > tol):
            return False
    return True


def mask(s: str, host: str = "", user: str = "", password: str = "") -> str:
    """Replace the camera host / user / password in a text (longest value first)."""
    out = s
    for v, rep in sorted(((host, "<cam>"), (user, "<user>"), (password, "***")), key=lambda t: -len(t[0])):
        if v:
            out = out.replace(v, rep)
    return out
