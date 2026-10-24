# -*- coding: utf-8 -*-
"""本地 BERT 语义筛选运行时。

模型与 exe 分开存放（约 237MB 的 ONNX 不进 git、不进 exe）：
  %LocalLow%\\MirrorSorter\\model\\bert_filter_v1\\
    model_fp32.onnx / vocab.txt / deploy_config.json / manifest.json

本模块只做四件事：
  1. 纯 Python 的 BERT WordPiece 分词（与训练时 transformers 逐 id 对齐）；
  2. ONNX Runtime 批量前向 + 上下文拼接（任务/视频/分区/UP/上文）；
  3. 结果缓存、运行状态与「试一下」所需的标签概率；
  4. 模型独立更新（GitHub Release 里的 *.manifest.json → 下载 → 校验 → 原子替换）。

没有任何网络或模型读取发生在 import 时 —— app.py 可以安全地顶层导入。
"""
import hashlib
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

LABELS = ["insult", "sarcasm", "flame", "low_quality", "misinfo", "disgust",
          "antisocial", "spam", "clickbait", "unpatriotic", "foreign_worship", "normal"]
TOXIC = [x for x in LABELS if x != "normal"]
WHY = {"insult": "辱骂", "sarcasm": "阴阳怪气", "flame": "引战", "low_quality": "低质",
       "misinfo": "明显错误", "disgust": "恶心人", "antisocial": "反社会",
       "spam": "刷屏广告", "clickbait": "标题党", "unpatriotic": "不爱国",
       "foreign_worship": "崇洋媚外", "normal": "正常"}
TASK_CN = {"dm": "弹幕", "cf": "评论", "video": "视频"}
MODEL_NAME = "bert_filter_v1"
MANIFEST = "manifest.json"
MODEL_FILE = "model_fp32.onnx"
TOKENIZER_FILE = "vocab.txt"
CONFIG_FILE = "deploy_config.json"
_REQUIRED = (MODEL_FILE, TOKENIZER_FILE, CONFIG_FILE)
_ALLOWED_FILES = set(_REQUIRED) | {MANIFEST}
_MAXLEN_DEFAULT = 128
_INFER_BATCH = 32
_CACHE_CAP = 30000
_WS = re.compile(r"\s+")

_DATA_DIR = ""
_APP_DIR = ""
_REPO = "Xblyymdf/MirrorSorter"
_UA = "MirrorSorter-bert/1.0"

_ENGINE = {"sess": None, "tok": None, "cfg": None, "dir": "", "err": "", "tried": False,
           "loaded_at": 0.0, "load_ms": 0, "labels": [], "infer_ms": 0, "infer_n": 0}
_ENGINE_LOCK = threading.Lock()
_INFER_LOCK = threading.Lock()
_CACHE = {}
_CACHE_SIG = [None]
_CACHE_LOCK = threading.Lock()

_MODEL_LOCK = threading.Lock()
_MODEL_LAST = {"info": None, "ts": 0.0}
_MODEL_STATE = {"phase": "idle", "received": 0, "total": 0, "file": "", "version": "",
                "error": "", "path": ""}
_MODEL_DOWNLOADING = threading.Event()

_MIRRORS = ("https://gh-proxy.com/", "https://ghfast.top/",
            "https://gh-proxy.org/", "https://ghproxy.net/")
_HOSTS = ("github.com", "objects.githubusercontent.com",
          "github-releases.githubusercontent.com", "release-assets.githubusercontent.com")


def init(data_dir, app_dir, repo=""):
    global _DATA_DIR, _APP_DIR, _REPO
    _DATA_DIR = str(data_dir or "")
    _APP_DIR = str(app_dir or "")
    if repo:
        _REPO = str(repo)
    _ENGINE["tried"] = False
    _ENGINE["err"] = ""


def _base_dirs():
    out = []
    env = os.environ.get("MIRRORSORTER_BERT_DIR")
    if env:
        out.append(env)
    if _DATA_DIR:
        out.append(os.path.join(_DATA_DIR, "model", MODEL_NAME))
    if _APP_DIR:
        out.append(os.path.join(_APP_DIR, "bert_model", MODEL_NAME))
        out.append(os.path.join(_APP_DIR, "model_bert", MODEL_NAME))
        out.append(os.path.join(_APP_DIR, "_bert", "models", MODEL_NAME))   # 源码开发
        out.append(os.path.join(_APP_DIR, "model", MODEL_NAME))
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        out.append(os.path.join(meipass, "model", MODEL_NAME))
    seen, uniq = set(), []
    for d in out:
        d = os.path.normcase(os.path.abspath(d))
        if d not in seen:
            seen.add(d)
            uniq.append(d)
    return uniq


def model_dir():
    """当前生效的模型目录（优先已完整安装者，否则返回第一个候选路径）。"""
    dirs = _base_dirs()
    for d in dirs:
        if all(os.path.isfile(os.path.join(d, f)) for f in _REQUIRED):
            return d
    return dirs[0] if dirs else ""


def install_dir():
    """自动更新安装位置：用户数据目录（exe 换版本不会丢模型）。"""
    if _DATA_DIR:
        return os.path.join(_DATA_DIR, "model", MODEL_NAME)
    return model_dir()


def _read_json(p, default=None):
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _model_version(d=None):
    d = d or model_dir()
    mf = _read_json(os.path.join(d, MANIFEST), {}) if d else {}
    if isinstance(mf, dict) and mf.get("version"):
        return str(mf["version"])
    cfg = _read_json(os.path.join(d, CONFIG_FILE), {}) if d else {}
    if isinstance(cfg, dict) and cfg.get("version"):
        return str(cfg["version"])
    return "1.0.0" if d and os.path.isfile(os.path.join(d, MODEL_FILE)) else ""


# ------------------------------------------------------------------ 分词
def _is_whitespace(ch):
    if ch in (" ", "\t", "\n", "\r"):
        return True
    return unicodedata.category(ch) == "Zs"


def _is_control(ch):
    if ch in ("\t", "\n", "\r"):
        return False
    return unicodedata.category(ch) == "Cc"


def _is_punct(ch):
    cp = ord(ch)
    if 33 <= cp <= 47 or 58 <= cp <= 64 or 91 <= cp <= 96 or 123 <= cp <= 126:
        return True
    return unicodedata.category(ch).startswith("P")


def _is_cjk(ch):
    cp = ord(ch)
    return (0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or
            0x20000 <= cp <= 0x2A6DF or 0x2A700 <= cp <= 0x2B73F or
            0x2B740 <= cp <= 0x2B81F or 0x2B820 <= cp <= 0x2CEAF or
            0xF900 <= cp <= 0xFAFF or 0x2F800 <= cp <= 0x2FA1F)


def _strip_accents(t):
    out = []
    for ch in unicodedata.normalize("NFD", t):
        if unicodedata.category(ch) == "Mn":
            continue
        out.append(ch)
    return "".join(out)


def _basic_tokens(text):
    text = unicodedata.normalize("NFC", str(text or ""))
    clean = []
    for ch in text:
        cp = ord(ch)
        if cp == 0 or cp == 0xFFFD or _is_control(ch):
            continue
        clean.append(" " if _is_whitespace(ch) else ch)
    text = "".join(clean)
    spaced = []
    for ch in text:
        if _is_cjk(ch):
            spaced.append(" ")
            spaced.append(ch)
            spaced.append(" ")
        else:
            spaced.append(ch)
    out = []
    for tok in "".join(spaced).split():
        tok = _strip_accents(tok.lower())
        cur = []
        for ch in tok:
            if _is_punct(ch):
                cur.append(" ")
                cur.append(ch)
                cur.append(" ")
            else:
                cur.append(ch)
        out.extend("".join(cur).split())
    return out


class _Tokenizer(object):
    def __init__(self, vocab_path):
        self.vocab = {}
        with open(vocab_path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                t = line.rstrip("\r\n")
                if t not in self.vocab:
                    self.vocab[t] = i
        self.unk = self.vocab.get("[UNK]", 100)
        self.cls = self.vocab.get("[CLS]", 101)
        self.sep = self.vocab.get("[SEP]", 102)
        self.pad = self.vocab.get("[PAD]", 0)

    def _wordpiece(self, tok):
        if len(tok) > 100:
            return [self.unk]
        out, start = [], 0
        while start < len(tok):
            end, cur = len(tok), None
            while start < end:
                piece = tok[start:end]
                if start > 0:
                    piece = "##" + piece
                if piece in self.vocab:
                    cur = piece
                    break
                end -= 1
            if cur is None:
                return [self.unk]
            out.append(cur)
            start = end
        return out

    def tokenize(self, text):
        ids = []
        for tok in _basic_tokens(text):
            for piece in self._wordpiece(tok):
                ids.append(self.vocab.get(piece, self.unk))
        return ids

    def encode_pair(self, ctx, text, maxlen, pad_to=None):
        a = self.tokenize(ctx)
        b = self.tokenize(text)
        room = max(3, int(maxlen) - 3)
        while len(a) + len(b) > room:
            if len(a) >= len(b):
                a.pop()
            else:
                b.pop()
        ids = [self.cls] + a + [self.sep] + b + [self.sep]
        tt = [0] * (len(a) + 2) + [1] * (len(b) + 1)
        mask = [1] * len(ids)
        # pad_to=None 补到 maxlen（与训练一致）；0/1 表示不补，由调用方按批内最长动态补齐
        target = int(maxlen) if pad_to is None else int(pad_to)
        pad = max(0, target - len(ids))
        return (ids + [self.pad] * pad, mask + [0] * pad, tt + [0] * pad)


# ------------------------------------------------------------------ 推理
def available():
    """模型文件齐不齐（不触发加载）。"""
    d = model_dir()
    return bool(d and all(os.path.isfile(os.path.join(d, f)) for f in _REQUIRED))


def load():
    """惰性加载模型，失败只记 err，绝不抛给调用方。"""
    with _ENGINE_LOCK:
        if _ENGINE["sess"] is not None:
            return _ENGINE["sess"]
        if _ENGINE["tried"] and _ENGINE["err"]:
            return None
        _ENGINE["tried"] = True
        d = model_dir()
        if not available():
            _ENGINE["dir"] = d
            _ENGINE["err"] = "模型未安装（需要 %s）" % MODEL_FILE
            return None
        t0 = time.perf_counter()
        try:
            import onnxruntime as ort
            cfg = _read_json(os.path.join(d, CONFIG_FILE), {}) or {}
            tok = _Tokenizer(os.path.join(d, TOKENIZER_FILE))
            so = ort.SessionOptions()
            rt = cfg.get("runtime") or {}
            so.intra_op_num_threads = max(1, min(8, int(rt.get("intra_op_threads") or 4)))
            so.inter_op_num_threads = max(1, min(4, int(rt.get("inter_op_threads") or 1)))
            so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            so.log_severity_level = 3
            sess = ort.InferenceSession(os.path.join(d, MODEL_FILE), sess_options=so,
                                        providers=["CPUExecutionProvider"])
            _ENGINE.update(sess=sess, tok=tok, cfg=cfg, dir=d, err="",
                           labels=list(cfg.get("labels") or LABELS),
                           loaded_at=time.time(),
                           load_ms=int((time.perf_counter() - t0) * 1000))
            # 首次前向会分配 workspace，放在加载阶段完成，避免第一条请求被拖慢
            try:
                _infer([{"text": "预热"}])
            except Exception:
                pass
        except Exception as e:
            _ENGINE["err"] = "%s: %s" % (type(e).__name__, str(e)[:180])
            _ENGINE["sess"] = None
        return _ENGINE["sess"]


def unload():
    with _ENGINE_LOCK:
        _ENGINE.update(sess=None, tok=None, cfg=None, tried=False, err="", loaded_at=0.0)


def reload():
    unload()
    return load() is not None


def ready():
    return _ENGINE["sess"] is not None or load() is not None


def _ctx_text(kind, ctx):
    c = ctx if isinstance(ctx, dict) else {}
    parts = ["任务:%s" % TASK_CN.get(kind or "cf", "评论")]
    if c.get("title"):
        parts.append("视频:%s" % str(c["title"])[:60])
    if c.get("tags"):
        parts.append("分区:%s" % str(c["tags"])[:16])
    if c.get("up"):
        parts.append("UP:%s" % str(c["up"])[:24])
    if c.get("parent"):
        parts.append("上文:%s" % str(c["parent"])[:80])
    return " | ".join(parts)


def _clean_text(t):
    t = _WS.sub(" ", str(t or "")).strip()
    return t.replace("\u200b", "").replace("\ufeff", "")


def _thresholds(point):
    cfg = _ENGINE.get("cfg") or {}
    pts = cfg.get("operating_points") or {}
    try:
        return float(pts.get(point or "strict") or pts.get("strict") or 0.9933)
    except Exception:
        return 0.9933


def _labels(sess, labels):
    known = set(_ENGINE.get("labels") or LABELS)
    got = [x for x in (labels or TOXIC) if x in known and x != "normal"]
    return got or [x for x in TOXIC if x in known]


def _infer_probs(items):
    """items: [{text, ctx, kind}] -> (numpy probs [n,12], names)。"""
    sess = _ENGINE.get("sess")
    tok = _ENGINE.get("tok")
    if sess is None or tok is None:
        return None, []
    import numpy as np
    maxlen = int((_ENGINE.get("cfg") or {}).get("maxlen") or _MAXLEN_DEFAULT)
    enc = [tok.encode_pair(_ctx_text(it.get("kind"), it.get("ctx")), _clean_text(it.get("text")),
                           maxlen, pad_to=1) for it in items]
    n = len(enc)
    # 动态补齐到批内最长: 训练时补到 128 只是批量便利，attention_mask 保证结果等价。
    # 实测 60 条上下文型输入 128 补齐要 2.1s，按批内长度只要 ~0.6s。
    seq = max(8, max(len(e[0]) for e in enc))
    for e in enc:
        d = seq - len(e[0])
        if d > 0:
            e[0].extend([tok.pad] * d)
            e[1].extend([0] * d)
            e[2].extend([0] * d)
    ids = np.asarray([e[0] for e in enc], dtype=np.int64)
    mask = np.asarray([e[1] for e in enc], dtype=np.int64)
    tt = np.asarray([e[2] for e in enc], dtype=np.int64)
    t0 = time.perf_counter()
    with _INFER_LOCK:
        logits = sess.run(None, {"input_ids": ids, "attention_mask": mask,
                                 "token_type_ids": tt})[0]
    dt = (time.perf_counter() - t0) * 1000.0
    _ENGINE["infer_ms"] = int(dt)
    _ENGINE["infer_n"] = n
    probs = 1.0 / (1.0 + np.exp(-logits.astype(np.float32)))
    return probs, (_ENGINE.get("labels") or LABELS)


def _infer(items):
    probs, names = _infer_probs(items)
    if probs is None:
        return []
    out = []
    for row in probs:
        best_i, best = -1, -1.0
        for i, nm in enumerate(names):
            if nm == "normal":
                continue
            if row[i] > best:
                best, best_i = float(row[i]), i
        out.append({"label": names[best_i] if best_i >= 0 else "",
                    "score": round(max(0.0, best), 6),
                    "why": WHY.get(names[best_i], names[best_i]) if best_i >= 0 else ""})
    return out


def scan(items, kind="cf", point="strict", labels=None, thresh=None):
    """批量判定，返回与 items 等长的 [{hit, why, label, score}]。

    items 元素可为字符串或 {text, title, tags, up, parent}。
    """
    norm = []
    for it in (items or []):
        if isinstance(it, dict):
            c = dict(it)
            c["text"] = _clean_text(c.get("text"))
        else:
            c = {"text": _clean_text(it)}
        norm.append(c)
    if not norm:
        return []
    if not ready():
        return [{"hit": False, "why": "", "label": "", "score": 0.0} for _ in norm]
    use = _labels(None, labels)
    try:
        thr = float(thresh) if thresh is not None else _thresholds(point)
    except Exception:
        thr = _thresholds(point)
    if not (0.0 < thr <= 1.0):
        thr = _thresholds(point)
    sig = (",".join(sorted(use)), round(thr, 4))
    if _CACHE_SIG[0] != sig:
        with _CACHE_LOCK:
            _CACHE.clear()
        _CACHE_SIG[0] = sig

    out = [None] * len(norm)
    todo, ctxs = [], []
    keys = []
    for i, it in enumerate(norm):
        txt = it.get("text") or ""
        ctx = it
        key = (kind, txt, str(ctx.get("title") or "")[:60], str(ctx.get("up") or "")[:24],
               str(ctx.get("tags") or "")[:16], str(ctx.get("parent") or "")[:80])
        if len(txt) < 2:
            out[i] = {"hit": False, "why": "", "label": "", "score": 0.0}
            continue
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
        if hit is not None:
            out[i] = dict(hit)
            continue
        keys.append(key)
        todo.append({"text": txt, "ctx": ctx, "kind": kind, "idx": i})
        ctxs.append(i)

    for off in range(0, len(todo), _INFER_BATCH):
        chunk = todo[off:off + _INFER_BATCH]
        try:
            res = _infer(chunk)
        except Exception:
            res = []
        for j, it in enumerate(chunk):
            r = res[j] if j < len(res) else {"label": "", "score": 0.0, "why": ""}
            picked = bool(r.get("label") in use and float(r.get("score") or 0) >= thr)
            rec = {"hit": picked, "why": r.get("why") if picked else "",
                   "label": r.get("label") or "", "score": float(r.get("score") or 0.0)}
            out[it["idx"]] = rec
            with _CACHE_LOCK:
                if len(_CACHE) < _CACHE_CAP:
                    _CACHE[keys[off + j] if off + j < len(keys) else
                           (kind, it["text"], "", "", "", "")] = dict(rec)
    for i, r in enumerate(out):
        if r is None:
            out[i] = {"hit": False, "why": "", "label": "", "score": 0.0}
    return out


def probe(text, kind="cf", ctx=None, point="strict", labels=None, thresh=None):
    """单条完整结果（给设置页「试一下」）：所有标签概率排名。"""
    if not load():
        return {"ok": False, "reason": _ENGINE["err"] or "模型未加载"}
    txt = _clean_text(text)
    if len(txt) < 2:
        return {"ok": False, "reason": "文本太短（不足 2 个字，不进模型）"}
    try:
        probs, names = _infer_probs([{"text": txt, "ctx": ctx or {}, "kind": kind}])
        p = probs[0]
    except Exception as e:
        return {"ok": False, "reason": "%s: %s" % (type(e).__name__, str(e)[:100])}
    use = _labels(None, labels)
    try:
        thr = float(thresh) if thresh is not None else _thresholds(point)
    except Exception:
        thr = _thresholds(point)
    if not (0.0 < thr <= 1.0):
        thr = _thresholds(point)
    rows = sorted(((float(p[i]), names[i]) for i in range(len(names))), reverse=True)
    best_p, best_l = rows[0]
    hit = bool(best_l in use and best_p >= thr)
    return {"ok": True, "text": txt, "label": best_l,
            "name": WHY.get(best_l, best_l), "score": round(best_p, 4),
            "hit": hit, "picked": best_l in use, "thresh": round(thr, 4),
            "why": WHY.get(best_l, best_l) if hit else "",
            "top": [{"label": l, "name": WHY.get(l, l), "score": round(s, 4)}
                    for s, l in rows[:5]]}


def status():
    """给设置页/更新弹窗的状态，不触发加载。"""
    d = model_dir()
    size = 0
    for f in _REQUIRED:
        p = os.path.join(d, f) if d else ""
        if p and os.path.isfile(p):
            size += os.path.getsize(p)
    rt = ""
    try:
        import onnxruntime
        rt = onnxruntime.__version__
    except Exception:
        pass
    return {"ok": True, "name": MODEL_NAME, "available": available(),
            "ready": _ENGINE["sess"] is not None,
            "engine_ready": bool(rt), "runtime": rt, "dir": d,
            "version": _model_version(d), "size_mb": round(size / 1048576.0, 1),
            "load_ms": _ENGINE["load_ms"], "infer_ms": _ENGINE["infer_ms"],
            "infer_n": _ENGINE["infer_n"], "err": _ENGINE["err"],
            "labels": list(_ENGINE.get("labels") or LABELS), "cache": len(_CACHE),
            "points": dict((_ENGINE.get("cfg") or {}).get("operating_points") or {})}


# ------------------------------------------------------------------ 模型更新
def _ver_tuple(v):
    m = re.match(r"^[vV]?(\d+(?:\.\d+)*)", str(v or "").strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def _ver_newer(a, b):
    x, y = _ver_tuple(a), _ver_tuple(b)
    if not x or not y:
        return False
    n = max(len(x), len(y))
    return x + (0,) * (n - len(x)) > y + (0,) * (n - len(y))


def _url_ok(url):
    try:
        p = urllib.parse.urlparse(str(url or ""))
        host = (p.hostname or "").lower()
        return p.scheme == "https" and (host in _HOSTS or host.endswith(".githubusercontent.com"))
    except Exception:
        return False


def _http_json(url, timeout=15):
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA, "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _http_bytes(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _fetch_manifest(url, sha="", timeout=8):
    """并发请求镜像与官方地址取清单, 先到先得。

    国内直连 release assets(对象存储域)常整段超时, 而 GitHub API 本身是通的 ——
    exe 下载早就走镜像, 清单却一直是裸直连。只有 API 给出 sha256 时才允许镜像,
    拿到字节必须与官方摘要一致, 否则丢弃; 没摘要就只走官方。"""
    sha = str(sha or "").lower()
    srcs = ([m + url for m in _MIRRORS] if sha else []) + [url]
    q = queue.Queue()

    def work(src):
        try:
            raw = _http_bytes(src, timeout=timeout)
            if sha and hashlib.sha256(raw).hexdigest().lower() != sha:
                raise ValueError("清单 sha256 校验失败")
            q.put((raw, None))
        except Exception as e:
            q.put((None, e))

    for src in srcs:
        threading.Thread(target=work, args=(src,), daemon=True).start()
    last = None
    deadline = time.time() + timeout + 2
    for _ in srcs:
        remain = deadline - time.time()
        if remain <= 0:
            break
        try:
            raw, err = q.get(timeout=remain)
        except queue.Empty:
            break
        if err is None:
            return json.loads(raw.decode("utf-8", "replace"))
        last = err
    raise last or RuntimeError("清单请求超时")


def _safe_name(n):
    n = os.path.basename(str(n or ""))
    return n if n in _ALLOWED_FILES else ""


def check_update():
    """从 GitHub Releases 里找最新的 *.manifest.json 模型清单。只读网络。"""
    cur = _model_version()
    try:
        rels = _http_json("https://api.github.com/repos/%s/releases?per_page=30" % _REPO)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            msg = "仓库还没有发布过版本。"
        elif e.code in (403, 429):
            msg = "GitHub 接口被限流，请稍后再试。"
        else:
            msg = "检查模型更新失败：HTTP %s" % e.code
        return {"ok": False, "current": cur, "error": msg}
    except Exception as e:
        return {"ok": False, "current": cur, "error": "检查模型更新失败：%s" % str(e)[:140]}
    cand = None
    for rel in (rels if isinstance(rels, list) else []):
        for a in (rel.get("assets") or []):
            name = str(a.get("name") or "")
            url = str(a.get("browser_download_url") or "")
            if name.lower().endswith(".manifest.json") and _url_ok(url):
                cand = (rel, a)
                break
        if cand:
            break
    if not cand:
        return {"ok": True, "current": cur, "latest": cur, "has_update": False,
                "error": "", "message": "模型仓库暂无发布。"}
    rel, asset = cand
    _dig = str(asset.get("digest") or "")
    _sha = _dig[7:] if _dig.lower().startswith("sha256:") else ""
    try:
        mf = _fetch_manifest(str(asset.get("browser_download_url")), _sha)
    except Exception as e:
        return {"ok": False, "current": cur, "error": "读取模型清单失败：%s" % str(e)[:140]}
    if not isinstance(mf, dict) or str(mf.get("name") or "") != MODEL_NAME:
        return {"ok": False, "current": cur, "error": "模型清单格式不对（name 不是 %s）" % MODEL_NAME}
    latest = str(mf.get("version") or rel.get("tag_name") or "").lstrip("v")
    assets = {}
    for a in (rel.get("assets") or []):
        nm = _safe_name(a.get("name"))
        if nm:
            assets[nm] = a
    files = []
    total = 0
    for it in (mf.get("files") or []):
        nm = _safe_name(it.get("name"))
        if not nm or nm == MANIFEST:
            continue
        a = assets.get(nm)
        if not a or not _url_ok(a.get("browser_download_url")):
            return {"ok": False, "current": cur, "error": "模型清单要求的文件未随 Release 发布：%s" % nm}
        size = int(it.get("size") or a.get("size") or 0)
        total += size
        files.append({"name": nm, "size": size,
                      "sha256": str(it.get("sha256") or "").lower(),
                      "url": str(a.get("browser_download_url"))})
    if not all(any(f["name"] == x for f in files) for x in _REQUIRED):
        return {"ok": False, "current": cur, "error": "模型清单缺少必需文件。"}
    info = {"ok": True, "current": cur, "latest": latest,
            "has_update": bool(latest and (not cur or _ver_newer(latest, cur))),
            "size": total, "files": files, "manifest": mf,
            "notes": str(mf.get("notes") or rel.get("body") or "")[:3000],
            "page": str(rel.get("html_url") or ""), "name": MODEL_NAME}
    with _MODEL_LOCK:
        _MODEL_LAST["info"] = info
        _MODEL_LAST["ts"] = time.time()
    return info


def progress():
    with _MODEL_LOCK:
        return {"ok": True, "phase": _MODEL_STATE["phase"],
                "received": _MODEL_STATE["received"], "total": _MODEL_STATE["total"],
                "file": _MODEL_STATE["file"], "version": _MODEL_STATE["version"],
                "error": _MODEL_STATE["error"], "path": _MODEL_STATE["path"]}


def _download_sources(url, sha):
    if sha and _url_ok(url):
        return [m + url for m in _MIRRORS] + [url]
    return [url] if _url_ok(url) else []


def _fetch_file(filename, url, size, sha, dst):
    srcs = _download_sources(url, sha)
    if not srcs:
        raise ValueError("下载地址不受信任：%s" % filename)
    last = ""
    for src in srcs:
        with _MODEL_LOCK:
            start_received = _MODEL_STATE["received"]
        try:
            req = urllib.request.Request(src, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=30) as r, open(dst, "wb") as f:
                while True:
                    b = r.read(256 * 1024)
                    if not b:
                        break
                    f.write(b)
                    with _MODEL_LOCK:
                        _MODEL_STATE["received"] += len(b)
            got = os.path.getsize(dst)
            if size and got != int(size):
                raise ValueError("大小不符：%d != %d" % (got, int(size)))
            if sha:
                h = hashlib.sha256()
                with open(dst, "rb") as f:
                    for b in iter(lambda: f.read(1 << 20), b""):
                        h.update(b)
                if h.hexdigest().lower() != sha:
                    raise ValueError("SHA256 校验失败")
            return True
        except Exception as e:
            last = "%s: %s" % (urllib.parse.urlparse(src).hostname or src, str(e)[:120])
            with _MODEL_LOCK:
                _MODEL_STATE["received"] = start_received
            try:
                if os.path.exists(dst):
                    os.remove(dst)
            except OSError:
                pass
    raise RuntimeError(last or "下载失败")


def _swap_install(tmp, target):
    bak = target + ".bak-%d" % int(time.time())
    unload()   # Windows 下 ONNX 会锁住模型文件，必须先释放
    if os.path.isdir(target):
        os.replace(target, bak)
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        os.replace(tmp, target)
    except Exception:
        if os.path.isdir(bak) and not os.path.isdir(target):
            os.replace(bak, target)
        raise
    if os.path.isdir(bak):
        try:
            shutil.rmtree(bak)
        except OSError:
            pass


def _download_worker(info):
    tmp = os.path.join(_DATA_DIR or _APP_DIR, ".model_download_%d" % int(time.time()))
    try:
        os.makedirs(tmp, exist_ok=True)
        for it in info["files"]:
            with _MODEL_LOCK:
                _MODEL_STATE["file"] = it["name"]
            _fetch_file(it["name"], it["url"], it["size"], it["sha256"],
                        os.path.join(tmp, it["name"]))
        with open(os.path.join(tmp, MANIFEST), "w", encoding="utf-8") as f:
            json.dump(info.get("manifest") or {}, f, ensure_ascii=False, indent=2)
        target = install_dir()
        _swap_install(tmp, target)
        with _MODEL_LOCK:
            _MODEL_STATE.update(phase="ready", file="", path=target,
                                version=info.get("latest") or "", error="")
        load()
    except Exception as e:
        with _MODEL_LOCK:
            _MODEL_STATE.update(phase="error", error="%s: %s" % (type(e).__name__, str(e)[:200]))
        shutil.rmtree(tmp, ignore_errors=True)
    finally:
        _MODEL_DOWNLOADING.clear()


def start_download():
    with _MODEL_LOCK:
        info = _MODEL_LAST.get("info")
    if not info or not info.get("files"):
        return {"ok": False, "error": "请先检查模型更新。"}
    if _MODEL_DOWNLOADING.is_set():
        return {"ok": False, "error": "模型正在下载中。"}
    if not _DATA_DIR:
        return {"ok": False, "error": "没有可写的数据目录。"}
    total = sum(int(f.get("size") or 0) for f in info["files"])
    with _MODEL_LOCK:
        _MODEL_STATE.update(phase="downloading", received=0, total=total, file="",
                            version=info.get("latest") or "", error="", path="")
    _MODEL_DOWNLOADING.set()
    threading.Thread(target=_download_worker, args=(dict(info),),
                     name="bert-model-dl", daemon=True).start()
    return {"ok": True}


def install_from_dir(src):
    """开发/离线安装：只复制必需文件，并写一份最小 manifest。"""
    target = install_dir()
    os.makedirs(target, exist_ok=True)
    for f in _REQUIRED:
        p = os.path.join(src, f)
        if not os.path.isfile(p):
            raise FileNotFoundError(p)
        shutil.copy2(p, os.path.join(target, f))
    mf = _read_json(os.path.join(src, MANIFEST), {}) or {}
    if not mf:
        mf = {"name": MODEL_NAME, "version": "1.0.0",
              "files": [{"name": f} for f in _REQUIRED]}
    with open(os.path.join(target, MANIFEST), "w", encoding="utf-8") as f:
        json.dump(mf, f, ensure_ascii=False, indent=2)
    reload()
    return target


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=os.path.join(
        os.path.expanduser("~"), "AppData", "LocalLow", "MirrorSorter"))
    ap.add_argument("--app-dir", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--install-from", default="")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    init(args.data_dir, args.app_dir)
    if args.install_from:
        print("installed ->", install_from_dir(args.install_from))
    if args.check:
        print(json.dumps(check_update(), ensure_ascii=False, indent=2))
    if args.status:
        print(json.dumps(status(), ensure_ascii=False, indent=2))
