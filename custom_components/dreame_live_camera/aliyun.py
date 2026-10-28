"""LinkVisual identity and relay negotiation, using verified HTTPS.

Protocol ported from MIT-licensed @apocaliss92/nodedreame; see NOTICE.
Constants identify the upstream public mobile client, not user credentials.
No request/response bodies or signed URLs are logged.
"""
import base64
import hashlib
import hmac
import json
import time
import uuid
from email.utils import formatdate
from urllib.parse import urlencode

from .const import VIDEO_STREAM_TYPE

APP_KEY = "33974273"
APP_SECRET = "65a62f03ae5eda97af962fecc2e3efa6"
REGIONS = {"eu":"eu-central-1", "de":"eu-central-1", "ru":"eu-central-1", "cn":"cn-shanghai", "tw":"cn-shanghai", "us":"us-west-1", "sg":"ap-southeast-1", "in":"ap-southeast-1"}


def signed(path, *, payload=None, form=None):
    content_type = "application/json; charset=UTF-8" if payload is not None else "application/x-www-form-urlencoded; charset=UTF-8"
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False) if payload is not None else urlencode(form)
    digest = base64.b64encode(hashlib.md5(body.encode()).digest()).decode() if payload is not None else ""
    date = formatdate(usegmt=True)
    headers = {"X-Ca-Key":APP_KEY, "X-Ca-Nonce":str(uuid.uuid4()).upper(), "X-Ca-Timestamp":str(int(time.time()*1000)), "X-Ca-Version":"1"}
    if payload is not None:
        headers["X-Ca-Stage"] = "RELEASE"
    names = sorted(headers, key=str.lower)
    resource = path
    if form:
        resource += "?" + "&".join(f"{k}={form[k]}" for k in sorted(form))
    signing = f"POST\napplication/json; charset=UTF-8\n{digest}\n{content_type}\n{date}\n" + "".join(f"{k}:{headers[k]}\n" for k in names) + resource
    headers.update({"Content-Type":content_type, "Accept":"application/json; charset=UTF-8", "Date":date, "X-Ca-Signature":base64.b64encode(hmac.new(APP_SECRET.encode(), signing.encode(), hashlib.sha1).digest()).decode(), "X-Ca-Signature-Method":"HmacSHA1", "X-Ca-Signature-Headers":",".join(names)})
    if digest:
        headers["Content-MD5"] = digest
    return headers, body


class VideoIdentity:
    def __init__(self, http, region):
        self.http, self.region = http, REGIONS[region]
        self.iot_token = None

    async def post(self, host, path, *, payload=None, form=None):
        headers, body = signed(path, payload=payload, form=form)
        async with self.http.post(f"https://{host}{path}", headers=headers, data=body, timeout=15) as response:
            if response.status != 200:
                raise RuntimeError("Video cloud HTTP request rejected")
            value = await response.json(content_type=None)
            if not isinstance(value, dict):
                raise RuntimeError("Invalid video cloud response")
            return value

    async def authenticate(self, auth_code):
        risk = {"createIfNotExist":"true", "sdkVersion":"3.4.0.27", "openaccountVersion":"3.4.0.27", "platformName":"iOS", "netType":"wifi", "brand":"Apple", "USE_H5_NC":"true", "deviceId":str(uuid.uuid4()), "locale":"en-US", "platformVersion":"26.5.2", "platform":"iPhone", "appKey":APP_KEY, "USE_OA_PWD_ENCRYPT":"true", "model":"iPhone16,2", "utdid":uuid.uuid4().hex[:20], "appVersion":"1895"}
        value = json.dumps({"riskControlInfo":risk, "oauthPlateform":23, "oauthAppKey":APP_KEY, "authCode":auth_code}, separators=(",", ":"))
        oa = await self.post(f"living-account.{self.region}.aliyuncs.com", "/api/prd/loginbyoauth.json", form={"loginByOauthRequest":value})
        sid = oa["data"]["data"]["loginSuccessResult"]["sid"]
        payload = {"id":str(uuid.uuid4()).upper(), "params":{"request":{"appKey":APP_KEY, "authCode":sid, "accountType":"OA_SESSION"}}, "request":{"apiVer":"1.0.4", "language":"en-US", "appKey":APP_KEY}, "version":"1.0.0"}
        result = await self.post(f"{self.region}.api-iot.aliyuncs.com", "/account/createSessionByAuthCode", payload=payload)
        if result.get("code") not in (0, 200):
            raise RuntimeError("Video identity rejected")
        self.iot_token = result["data"]["iotToken"]

    async def _stream_info(self, channel_id):
        payload = {"id":str(uuid.uuid4()).upper(), "params":{"streamType":VIDEO_STREAM_TYPE, "needDomainName":True, "relayEncryptType":0, "clientType":"iOS", "iotId":channel_id, "enablePortPredict":True, "enableWebSocket":True, "forceIFrame":True, "cacheDuration":3000, "relayEncrypted":False}, "request":{"language":"en-US", "appKey":APP_KEY, "iotToken":self.iot_token, "apiVer":"2.2.0"}, "version":"1.0.0"}
        result = await self.post(f"{self.region}.api-iot.aliyuncs.com", "/vision/customer/stream/query", payload=payload)
        if result.get("code") not in (0, 200):
            raise RuntimeError("Video negotiation rejected")
        return result.get("data", {})

    async def offer(self, channel_id):
        data = await self._stream_info(channel_id)
        signal_url = (data.get("p2pInfo") or {}).get("signalUrl")
        if not signal_url:
            raise RuntimeError("Direct LAN video unavailable")
        return signal_url

    async def relay(self, channel_id):
        """Request a fresh relay URL only when direct video has failed."""
        data = await self._stream_info(channel_id)
        url = data.get("relayUrl")
        if not url or data.get("relayDecryptKey"):
            raise RuntimeError("Video relay unavailable")
        return url
