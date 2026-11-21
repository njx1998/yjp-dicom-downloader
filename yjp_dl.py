#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通用医疗影像云下载工具（海纳医信 / HinaPACS 平台，如北京友谊医院 yjp.bfh.com.cn）

用法：
    python yjp_dl.py "分享链接"                 # 下载全部序列并打包 ZIP
    python yjp_dl.py "分享链接" -o D:\\影像      # 指定输出目录
    python yjp_dl.py "分享链接" --series 2,3    # 只下载指定序列
    python yjp_dl.py "分享链接" --max 20        # 只下载前 20 幅（测试用）
    python yjp_dl.py "分享链接" --no-zip --no-preview

支持的链接形式：
    https://<主机>:8080/Study/ViewShared/<guid>
    https://<主机>:8080/Study/ViewImage?studyId=<uid>
    https://<主机>:8000/entry/viewimage?token=<token>
    或直接输入 studyId（1.2.826...）

依赖：pip install requests pydicom numpy pillow
"""

import argparse
import html
import json
import os
import re
import shutil
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import requests
import urllib3
from requests.adapters import HTTPAdapter
from pydicom import dcmread
from pydicom.datadict import dictionary_VR
from pydicom.dataelem import DataElement
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.tag import Tag
from pydicom.uid import ExplicitVRLittleEndian

urllib3.disable_warnings()

PS = "\\"  # DICOM 多值分隔符
IMPL_UID = "1.2.826.0.1.3680043.8.920.999.1"


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- 会话与认证
class Session:
    """负责建立会话、解析影像集、401 时自动重新登录"""

    def __init__(self, link, base8080="https://yjp.bfh.com.cn:8080",
                 base8000="https://yjp.bfh.com.cn:8000", workers=8):
        self.link = link.strip()
        self.base8080 = base8080.rstrip("/")
        self.base8000 = base8000.rstrip("/")
        self.workers = workers
        self.s = requests.Session()
        self.s.verify = False
        # 连接池必须 >= 并发数，否则 urllib3 会反复丢弃/重建 TLS 连接
        pool = workers * 2 + 8
        adapter = HTTPAdapter(pool_connections=pool, pool_maxsize=pool, max_retries=0)
        self.s.mount("https://", adapter)
        self.s.mount("http://", adapter)
        self.s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "X-Requested-With": "XMLHttpRequest",
            "Connection": "keep-alive",
        })
        self.lock = threading.Lock()
        self.epoch = 0
        self.ck = None
        self.study_id = None
        self.accession = None
        self.patient = {}
        self.imageset = None

    def _parse_shared_page(self, text):
        """从分享页提取患者信息（先剥离HTML标签）"""
        plain = re.sub(r"<[^>]+>", "\n", text)
        plain = re.sub(r"&nbsp;?", " ", plain)
        pat = {
            "patient": r"患者姓名\s*\n*\s*([^\n\s]+)",
            "accession": r"检查号\s*\n*\s*(\w+)",
            "modality": r"检查类型\s*\n*\s*(\w+)",
            "study_time": r"检查时间\s*\n*\s*([\d\-/: ]+)",
            "desc": r"影像描述\s*\n*\s*([^\n]+)",
        }
        for k, p in pat.items():
            m = re.search(p, plain)
            if m:
                v = m.group(1).strip()
                if v and v not in ("", "-"):
                    self.patient[k] = v

    def _resolve_token(self):
        """解析出一次性访问 token；返回 (token, 患者信息已尽力填充)"""
        link = self.link
        # 已经是 8000 的 entry token 链接，直接使用（不能重复请求，token 一次性）
        m = re.search(r"/entry/viewimage\?token=([A-Za-z0-9]+)", link)
        if m:
            return m.group(1)

        if "/ViewShared/" in link:
            r = self.s.get(link, timeout=60)
            r.encoding = "utf-8"
            self._parse_shared_page(r.text)
            m = re.search(r"ViewImage\?studyId=([0-9A-Za-z.\-]+)", r.text)
            if not m:
                raise RuntimeError("分享页中未找到影像入口，链接可能已失效")
            view = f"{self.base8080}/Study/ViewImage?studyId={m.group(1)}"
        elif "/Study/ViewImage" in link:
            view = link
        elif re.fullmatch(r"[0-9A-Za-z.\-]+", link):
            raise RuntimeError("仅输入 studyId 无法匿名访问，请使用医院给的分享链接")
        else:
            view = link

        r = self.s.get(view, timeout=60)
        r.encoding = "utf-8"
        if not self.patient:
            self._parse_shared_page(r.text)
        m = re.search(r"token=([A-Za-z0-9]+)", r.text)
        if not m:
            raise RuntimeError("未获取到影像访问 token，链接可能已过期或需要登录")
        return m.group(1)

    def login(self):
        """建立会话：取得 token -> 8000 查看器 -> 提取 ck / studyId"""
        with self.lock:
            return self._login_locked()

    def refresh_login(self, seen_epoch):
        """token 过期时只重登一次；其余线程直接复用刚刷新的会话"""
        with self.lock:
            if self.epoch != seen_epoch:
                return True
            return self._login_locked()

    def _login_locked(self):
        try:
            token = self._resolve_token()
            r = self.s.get(f"{self.base8000}/entry/viewimage?token={token}", timeout=60)
            r.encoding = "utf-8"
            m = re.search(r'window\.location\.href\s*=\s*"([^"]*)"', r.text)
            if not m:
                raise RuntimeError("入口页提示：访问地址无效或已过期，请重新打开分享链接")
            sv = f"{self.base8000}{m.group(1)}"
            r = self.s.get(sv, timeout=60)
            r.encoding = "utf-8"
            ck = re.search(r'LOAD_IMAGE_CACHE_KEY\s*=\s*"([0-9a-f]+)"', r.text)
            sid = re.search(r"studyId:\s*['\"]([0-9A-Za-z.\-]+)['\"]", r.text)
            acc = re.search(r'ACCESSION_NUMBER\s*=\s*["\']?([0-9A-Za-z]+)', r.text)
            if not ck or not sid:
                raise RuntimeError("查看器页面解析失败，页面结构可能已更新")
            self.ck, self.study_id = ck.group(1), sid.group(1)
            self.accession = acc.group(1) if acc else (self.patient.get("accession") or "")
            self.epoch += 1
            log(f"登录成功  studyId={self.study_id}  accession={self.accession}")
            return True
        except Exception as e:
            log(f"登录失败: {e}")
            return False

    def _get(self, url, timeout=120, retries=3):
        """带退避重试 + 单次重登的 GET；401 时同代际的并发请求只触发一次登录"""
        backoff = 0.5
        for _ in range(retries):
            epoch = self.epoch
            try:
                r = self.s.get(url, timeout=timeout)
            except Exception:
                time.sleep(backoff)
                backoff = min(backoff * 2, 4)
                continue
            if r.status_code == 200:
                return r
            if r.status_code == 401:
                self.refresh_login(epoch)
                continue
            time.sleep(backoff)
            backoff = min(backoff * 2, 4)
        return None

    def get_json(self, url, timeout=120):
        r = self._get(url, timeout)
        if r is None:
            return None
        try:
            return r.json()
        except Exception:
            return None

    def load_imageset(self):
        sid = self.study_id
        u = (f"{self.base8000}/ImageViewer/GetImageSet?studyId={sid}&imageSetId={sid}"
             + (f"&accessionNumber={self.accession}" if self.accession else "") + "&minThickness=5")
        data = self.get_json(u)
        if not data:
            raise RuntimeError("获取影像集失败")
        self.imageset = data
        return data

    # -- 单幅图像
    def fetch_pixel(self, img, timeout=90):
        exp = int(img["rows"]) * int(img["columns"]) * 2
        backoff = 0.3
        for _ in range(4):
            epoch = self.epoch
            # 每轮重建 URL：重登后 ck/studyId 已变，沿用旧 URL 会一路 401
            u = (f"{self.base8000}/ImageViewer/PixelDataJ2k?studyId={self.study_id}"
                 f"&imageId={img['imageId']}&frame={img.get('frame', 0)}"
                 f"&imageQuality=3&includeMetaData=true&isPrefetchRequest=false"
                 f"&ck={self.ck}&storageNodes={img.get('storageNode', '')}")
            try:
                r = self.s.get(u, timeout=timeout)
                if r.status_code == 200 and len(r.content) >= exp:
                    return r.content[:exp]
                if r.status_code == 401:
                    self.refresh_login(epoch)
                    continue
            except Exception:
                pass
            time.sleep(backoff)
            backoff = min(backoff * 2, 2)
        return None

    def fetch_tags(self, img, timeout=60):
        backoff = 0.3
        for _ in range(4):
            epoch = self.epoch
            u = (f"{self.base8000}/ImageViewer/GetImageDicomTags?studyId={self.study_id}"
                 f"&imageId={img['imageId']}&frame={img.get('frame', 0)}"
                 f"&storageNode={img.get('storageNode', '')}")
            try:
                r = self.s.get(u, timeout=timeout)
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 401:
                    self.refresh_login(epoch)
                    continue
            except Exception:
                pass
            time.sleep(backoff)
            backoff = min(backoff * 2, 2)
        return None


# ---------------------------------------------------------------- DICOM 重建
INT_VRS = {"US", "SS", "UL", "SL"}
FLT_VRS = {"FL", "FD"}


def sanitize(name):
    for ch in '\\/:*?"<>| ':
        name = name.replace(ch, "_")
    return name.strip("_") or "Series"


def atomic_write(path, data, text=False):
    """先写临时文件再原子替换，中断时不会留下半截“有效”文件"""
    tmp = f"{path}.tmp"
    if text:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
    else:
        with open(tmp, "wb") as f:
            f.write(data)
    os.replace(tmp, path)


def valid_dcm(path):
    """轻量校验：文件长度足够且偏移 128 处有 DICM 前缀"""
    try:
        if os.path.getsize(path) < 132:
            return False
        with open(path, "rb") as f:
            f.seek(128)
            return f.read(4) == b"DICM"
    except OSError:
        return False


def set_element(ds, tagstr, val, signed):
    g, e = tagstr.split(",")
    tag = Tag(int(g, 16), int(e, 16))
    try:
        vr = dictionary_VR(tag)
    except Exception:
        return False
    if vr in ("US or SS", "US or SS or OW"):
        vr = "SS" if signed else "US"
    elif " or " in vr:
        parts = vr.split(" or ")
        vr = next((p for p in parts if p not in ("OW", "OB")), parts[0])
    if vr == "SQ" or isinstance(val, (dict, list)) or vr in ("OB", "OW", "UN"):
        return False
    if vr in INT_VRS:
        nums = [int(float(p)) for p in str(val).split(PS)]
        val = nums[0] if len(nums) == 1 else nums
    elif vr in FLT_VRS:
        nums = [float(p) for p in str(val).split(PS)]
        val = nums[0] if len(nums) == 1 else nums
    ds[tag] = DataElement(tag, vr, val)
    return True


def build_dicom(tags, raw, out_path):
    ds = Dataset()
    pre = {t["tag"].lower(): t["value"] for t in tags}
    signed = str(pre.get("0028,0103", "0")) == "1"
    for t in tags:
        set_element(ds, t["tag"], t["value"], signed)
    rows, cols = int(ds.Rows), int(ds.Columns)
    n = rows * cols * 2
    ds[0x7FE00010] = DataElement(Tag(0x7FE0, 0x0010), "OW", raw[:n])
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = ds.SOPClassUID
    fm.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    fm.ImplementationClassUID = IMPL_UID
    fm.FileMetaInformationVersion = b"\x00\x01"
    ds.file_meta = fm
    ds.save_as(out_path, enforce_file_format=True)


# ---------------------------------------------------------------- 主流程
def run(args):
    sess = Session(args.link, args.base8080, args.base8000, args.workers)
    if not sess.login():
        return 1
    iset = sess.load_imageset()
    storage = iset.get("storageNode") or iset.get("StorageNode") or ""

    series = []
    for d in iset["displaySets"]:
        n = len(d["images"])
        series.append({"num": int(d["seriesNumber"]), "desc": d.get("description", ""), "n": n})
    series.sort(key=lambda x: x["num"])
    log(f"共 {sum(s['n'] for s in series)} 幅图像 / {len(series)} 个序列")
    for s in series:
        log(f"  序列 {s['num']:>3}  {s['desc']}  {s['n']} 幅")

    want = None
    if args.series:
        want = {int(x) for x in re.split(r"[,，\s]+", args.series) if x.strip()}

    # 输出目录
    pname = sess.patient.get("patient") or iset.get("patientName") or "未知患者"
    acc = sess.accession or iset.get("accessionNumber") or ""
    date = iset.get("studyDate", "")
    folder = sanitize(f"{pname}_{acc}_{date}")
    root = os.path.join(args.out, folder)
    tmp = os.path.join(root, ".cache")
    os.makedirs(tmp, exist_ok=True)
    log(f"输出目录: {root}")

    tasks = []
    for d in iset["displaySets"]:
        snum = int(d["seriesNumber"])
        if want and snum not in want:
            continue
        for img in d["images"]:
            img = dict(img)
            img["storageNode"] = storage
            tasks.append((snum, d.get("description", ""), img))
    if args.max:
        tasks = tasks[:args.max]
    total = len(tasks)

    # ---- 下载（支持断点续传；标签与像素并发，边下边重建）
    built = {}
    built_lock = threading.Lock()

    def build_one(snum, desc, img):
        """把缓存中的像素 + 标签重建为 DICOM（下载线程内直接做，与后续下载重叠）"""
        key = f"{snum:03d}_{img['instanceNumber']:05d}_{img.get('frame', 0)}"
        raw_p, tag_p = f"{tmp}/{key}.bin", f"{tmp}/{key}.json"
        if not (os.path.exists(raw_p) and os.path.exists(tag_p)):
            return
        sdir = os.path.join(root, f"Series_{snum:03d}_{sanitize(desc or str(snum))}")
        os.makedirs(sdir, exist_ok=True)
        fr = int(img.get("frame", 0) or 0)
        fname = (f"IM{int(img['instanceNumber']):05d}.dcm" if fr == 0
                 else f"IM{int(img['instanceNumber']):05d}_F{fr+1:02d}.dcm")
        out = os.path.join(sdir, fname)
        try:
            if not valid_dcm(out):
                with open(raw_p, "rb") as f:
                    raw = f.read()
                with open(tag_p, encoding="utf-8") as f:
                    tags = json.load(f)
                tmp_out = f"{out}.tmp"
                try:
                    build_dicom(tags, raw, tmp_out)
                    os.replace(tmp_out, out)
                finally:
                    if os.path.exists(tmp_out):
                        os.remove(tmp_out)
            with built_lock:
                built[key] = out
        except Exception as e:
            log(f"重建失败 {key}: {type(e).__name__} {e}")

    def work(item):
        snum, desc, img = item
        key = f"{snum:03d}_{img['instanceNumber']:05d}_{img.get('frame', 0)}"
        raw_p, tag_p = f"{tmp}/{key}.bin", f"{tmp}/{key}.json"
        exp = int(img["rows"]) * int(img["columns"]) * 2
        # 缓存损坏（中断留下的半截 json）视为不存在，重下
        tag_ok = False
        if os.path.exists(tag_p):
            try:
                with open(tag_p, encoding="utf-8") as f:
                    json.load(f)
                tag_ok = True
            except Exception:
                try:
                    os.remove(tag_p)
                except OSError:
                    pass
        # 标签请求先发出去，与像素下载并行，省掉一次串行往返
        tag_fut = None if tag_ok else tag_ex.submit(sess.fetch_tags, img)
        if not (os.path.exists(raw_p) and os.path.getsize(raw_p) >= exp):
            px = sess.fetch_pixel(img)
            if px is None:
                if tag_fut is not None:
                    tag_fut.cancel()
                return ("PIXFAIL", key)
            atomic_write(raw_p, px)
        if tag_fut is not None:
            tg = tag_fut.result()
            if tg is None:
                return ("TAGFAIL", key)
            atomic_write(tag_p, json.dumps(tg), text=True)
        build_one(snum, desc, img)
        return ("OK", key)

    done = fails = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(4, args.workers // 2)) as tag_ex:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(work, t): t for t in tasks}
            for f in as_completed(futs):
                try:
                    st, key = f.result()
                except Exception as e:
                    st, key = "ERROR", str(e)
                if st == "OK":
                    done += 1
                else:
                    fails += 1
                    log(f"失败 {st} {key}")
                if (done + fails) % 50 == 0:
                    rate = (done + fails) / max(time.time() - t0, 1) * 60
                    eta = (total - done - fails) / rate if rate > 0 else 0
                    log(f"进度 {done+fails}/{total}  成功{done} 失败{fails}  {rate:.0f}幅/分  "
                        f"剩余约 {eta:.0f} 分钟")
    log(f"下载完成: 成功 {done} / {total}，失败 {fails}")

    # ---- 补齐未重建的（重建失败或像素重试成功的）
    miss = [t for t in tasks
            if f"{t[0]:03d}_{t[2]['instanceNumber']:05d}_{t[2].get('frame', 0)}" not in built]
    if miss:
        log(f"补齐重建 {len(miss)} 幅 ...")
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            list(ex.map(lambda t: build_one(*t), miss))
    log(f"DICOM 重建完成 {len(built)} 幅")

    # ---- 预览图 + 汇总页
    if not args.no_preview:
        try:
            make_report(root, series, tasks, built, sess, iset)
            log("预览图与汇总页已生成")
        except Exception as e:
            log(f"预览生成失败: {e}")

    # ---- 打包
    if not args.no_zip:
        zp = root + "_DICOM.zip"
        log(f"正在打包 {os.path.basename(zp)} ...")
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for dirpath, _, files in os.walk(root):
                if ".cache" in dirpath:
                    continue
                for fn in files:
                    fp = os.path.join(dirpath, fn)
                    z.write(fp, os.path.relpath(fp, os.path.dirname(root)))
        log(f"打包完成: {zp} ({os.path.getsize(zp)/1e6:.0f} MB)")

    if not args.keep_cache:
        shutil.rmtree(tmp, ignore_errors=True)
    log("全部完成 ✔")
    return 0


def make_report(root, series, tasks, built, sess, iset):
    """生成各序列中间层预览图 + 汇总 HTML"""
    from PIL import Image

    prev_dir = os.path.join(root, "预览图")
    os.makedirs(prev_dir, exist_ok=True)
    rows = []
    for s in series:
        sdir = os.path.join(root, f"Series_{s['num']:03d}_{sanitize(s['desc'] or str(s['num']))}")
        if not os.path.isdir(sdir):
            continue
        files = sorted(f for f in os.listdir(sdir) if f.endswith(".dcm"))
        if not files:
            continue
        mid = os.path.join(sdir, files[len(files) // 2])
        ds = dcmread(mid)
        arr = ds.pixel_array.astype(np.float32)
        arr = arr * float(getattr(ds, "RescaleSlope", 1)) + float(getattr(ds, "RescaleIntercept", 0))
        wc, ww = ds.WindowCenter, ds.WindowWidth
        wc = float(wc[0] if isinstance(wc, (list, tuple)) or hasattr(wc, "__len__") and not isinstance(wc, str) else wc)
        ww = float(ww[0] if isinstance(ww, (list, tuple)) or hasattr(ww, "__len__") and not isinstance(ww, str) else ww)
        lo, hi = wc - ww / 2, wc + ww / 2
        img = np.clip(arr, lo, hi)
        img = ((img - lo) / max(hi - lo, 1) * 255).astype(np.uint8)
        png = os.path.join(prev_dir, f"Series_{s['num']:03d}_中间层.png")
        Image.fromarray(img).save(png)
        rows.append(f"<tr><td>{s['num']}</td><td>{html.escape(s['desc'])}</td><td>{len(files)}</td>"
                    f"<td>{float(getattr(ds, 'SliceThickness', 0) or 0):.2f} mm</td>"
                    f"<td>{getattr(ds, 'KVP', '')}</td><td>{wc:.0f} / {ww:.0f}</td>"
                    f"<td><img src='预览图/{os.path.basename(png)}' width='150'></td></tr>")

    pname = sess.patient.get("patient") or iset.get("patientName", "")
    doc = f"""<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'>
<title>电子影像汇总 - {html.escape(pname)}</title><style>
body{{font-family:'Microsoft Yahei',sans-serif;background:#f5f7fa;padding:24px;margin:0}}
.card{{background:#fff;border-radius:10px;padding:24px;max-width:1000px;margin:auto;box-shadow:0 2px 8px #0001}}
h1{{font-size:21px;margin:0 0 6px}}.sub{{color:#666;font-size:13px;margin-bottom:16px}}
table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{border:1px solid #e0e4ea;padding:7px 9px;text-align:left}}
th{{background:#eef2f7}}.note{{margin-top:16px;font-size:13px;color:#555;line-height:1.8}}
</style></head><body><div class='card'>
<h1>电子影像汇总</h1><div class='sub'>患者：{html.escape(pname)} ｜ 检查号：{html.escape(str(sess.accession or ''))}
 ｜ 检查：{html.escape(iset.get('modality', ''))} {html.escape(iset.get('studyDescription', ''))}
 ｜ 日期：{html.escape(iset.get('studyDate', ''))}</div>
<table><tr><th>序列号</th><th>描述</th><th>图像数</th><th>层厚</th><th>kV</th><th>窗位/窗宽</th><th>预览</th></tr>
{''.join(rows)}</table>
<div class='note'>共 <b>{len(built)}</b> 幅标准 DICOM 图像。可用 RadiAnt / MicroDicom / Horos / OSMIx 打开整个序列文件夹浏览或三维重建。</div>
</div></body></html>"""
    open(os.path.join(root, "影像汇总.html"), "w", encoding="utf-8").write(doc)


def main():
    ap = argparse.ArgumentParser(description="医疗影像云分享链接下载工具（输出标准 DICOM）")
    ap.add_argument("link", help="分享链接 / 查看链接 / studyId")
    ap.add_argument("-o", "--out", default="下载结果", help="输出目录（默认 ./下载结果）")
    ap.add_argument("-j", "--workers", type=int, default=12, help="并发数（默认 12，网络好可加到 16~24）")
    ap.add_argument("--series", help="只下载指定序列号，逗号分隔，如 2,3,303")
    ap.add_argument("--max", type=int, help="最多下载多少幅（调试用）")
    ap.add_argument("--no-zip", action="store_true", help="不打包 ZIP")
    ap.add_argument("--no-preview", action="store_true", help="不生成预览图与汇总页")
    ap.add_argument("--keep-cache", action="store_true", help="保留中间缓存（便于断点续传）")
    ap.add_argument("--base8080", default="https://yjp.bfh.com.cn:8080")
    ap.add_argument("--base8000", default="https://yjp.bfh.com.cn:8000")
    args = ap.parse_args()
    try:
        sys.exit(run(args))
    except KeyboardInterrupt:
        log("已中断（再次运行同一命令可断点续传）")
        sys.exit(130)


if __name__ == "__main__":
    main()
