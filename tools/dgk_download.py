"""공공데이터포털 파일데이터를 로그인 없이 내려받는다. 캡차가 요구되면 멈춘다."""
import json, re, subprocess, sys, urllib.parse, pathlib

BASE = "https://www.data.go.kr"
OUT = pathlib.Path(sys.argv[1])

def curl(*args):
    r = subprocess.run(["curl", "-sL", "--max-time", "600", *args], capture_output=True)
    return r.stdout

for pk in sys.argv[2:]:
    page = curl(f"{BASE}/data/{pk}/fileData.do").decode("utf-8", "replace")
    m = re.search(r"fn_fileDataDown\('(\d+)', '(uddi:[^']+)', '([^']*)','(\d+)', '(\d+)'\)", page)
    title = re.search(r'<meta name="title" property="og:title" content="([^"]*)"', page)
    if not m:
        print(f"{pk}\tNO_DOWNLOAD_BUTTON"); continue
    meta = json.loads(curl("-X", "POST", f"{BASE}/tcs/dss/selectFileDataDownload.do", "--data",
        urllib.parse.urlencode({"publicDataDetailPk": m.group(2), "publicDataPk": pk, "atchFileId": m.group(3),
                                "fileDetailSn": m.group(4), "publicDataTyCode": "PR0051"})))
    if not meta.get("status"):
        print(f"{pk}\tMETA_FAIL\t{meta.get('error')}"); continue
    aid, sn = meta["atchFileId"], meta["fileDetailSn"]
    name = meta["dataSetFileDetailInfo"].get("dataNm") or pk
    lim = json.loads(curl("-X", "POST", f"{BASE}/cmm/cmm/check-limit.json", "--data", f"atchFileId={aid}&fileDetailSn={sn}") or b"{}")
    if lim.get("needCaptcha"):
        print(f"{pk}\tCAPTCHA_REQUIRED\t{name}"); break
    hdr = OUT / f"{pk}.hdr"
    tmp = OUT / f"{pk}.part"
    subprocess.run(["curl", "-sL", "--max-time", "900", "-D", str(hdr), "-o", str(tmp),
                    f"{BASE}/cmm/cmm/fileDownload.do?atchFileId={aid}&fileDetailSn={sn}&dataNm={urllib.parse.quote(name)}"], check=True)
    h = hdr.read_text(errors="replace")
    fn = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";\r\n]+)', h, re.I)
    ext = pathlib.Path(urllib.parse.unquote(fn.group(1))).suffix if fn else ".bin"
    dest = OUT / f"{pk}{ext.lower()}"
    tmp.rename(dest); hdr.unlink()
    print(f"{pk}\t{dest.name}\t{dest.stat().st_size}\t{name}")
