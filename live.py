"""자동 추천 창: 롤 클라이언트 픽창을 읽어서 추천을 자동으로 갱신 (설치할 패키지 없음)

- 롤 클라이언트가 켜져 있으면 픽창(내 라인, 우리 팀, 상대 픽, 밴)을 1초마다 읽어요.
  클라이언트가 내 컴퓨터 안에서 여는 통로(LCU API)를 읽기만 하고, 클라이언트를 조작하지 않아요.
- 클라이언트가 없거나 픽창이 아니면 창에 직접 입력해서 쓸 수 있어요(수동 모드).

실행: 자동추천.bat 더블클릭, 또는  python live.py
시험: python live.py --demo   (가짜 픽창으로 화면 확인)
"""
import base64
import json
import math
import os
import re
import ssl
import subprocess
import sys
import threading
import time
import urllib.request

from recommend import HERE, ROLE_KO, ROLE_ALIASES, EMPTY, Model, norm

POSITION = {"top": 0, "jungle": 1, "middle": 2, "bottom": 3, "utility": 4}
LOCKFILES = [rf"{d}:\{base}\League of Legends\lockfile"
             for d in "CDEFG"
             for base in ("Riot Games", r"Program Files\Riot Games", r"Program Files (x86)\Riot Games", r"Games\Riot Games")]


# ---------------------------------------------------------------- 클라이언트 연결 (읽기 전용)

class Client:
    def __init__(self):
        self.port = self.token = None
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False          # 127.0.0.1의 클라이언트 자체 인증서라 검증 생략 (내 컴퓨터 안 통신만)
        self.ctx.verify_mode = ssl.CERT_NONE

    def _find(self):
        # 1) 실행 중인 클라이언트의 명령줄에서 포트·토큰 읽기
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"name='LeagueClientUx.exe'\" | Select-Object -ExpandProperty CommandLine"],
                capture_output=True, text=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            port = re.search(r"--app-port=(\d+)", out or "")
            tok = re.search(r"--remoting-auth-token=([\w-]+)", out or "")
            if port and tok:
                return port.group(1), tok.group(1)
        except Exception:
            pass
        try:                                  # PowerShell이 막힌 PC(피방 등)용
            out = subprocess.run(["wmic", "process", "where", "name='LeagueClientUx.exe'", "get", "commandline"],
                                 capture_output=True, text=True, timeout=10,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            port = re.search(r"--app-port=(\d+)", out or "")
            tok = re.search(r"--remoting-auth-token=([\w-]+)", out or "")
            if port and tok:
                return port.group(1), tok.group(1)
        except Exception:
            pass
        # 2) 설치 폴더의 lockfile
        for p in LOCKFILES:
            try:
                with open(p, encoding="utf-8") as f:
                    parts = f.read().strip().split(":")
                return parts[2], parts[3]
            except Exception:
                continue
        return None

    def get(self, path):
        if not self.port:
            found = self._find()
            if not found:
                return None, "롤 클라이언트를 못 찾았어요 (꺼져 있으면 수동 모드로 쓰세요)"
            self.port, self.token = found
        req = urllib.request.Request(f"https://127.0.0.1:{self.port}{path}")
        auth = base64.b64encode(f"riot:{self.token}".encode()).decode()
        req.add_header("Authorization", f"Basic {auth}")
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=3) as r:
                return json.load(r), "ok"
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None, "픽창 대기 중 (클라이언트 연결됨)"
            self.port = None
            return None, f"클라이언트 응답 오류 {e.code}"
        except Exception:
            self.port = None                      # 클라이언트를 껐다 켰으면 다시 찾기
            return None, "롤 클라이언트를 못 찾았어요 (꺼져 있으면 수동 모드로 쓰세요)"


def parse_session(s):
    """픽창 JSON -> (내 라인, 우리 팀 5칸, 내가 고른 챔피언, 상대 픽 목록, 밴 목록). 라인을 모르면 내 라인 None."""
    me = s.get("localPlayerCellId")
    my_role, my_champ = None, None
    allies = [None] * 5
    unplaced = []
    for p in s.get("myTeam", []):
        cid = p.get("championId") or 0
        pos = POSITION.get((p.get("assignedPosition") or "").lower())
        if p.get("cellId") == me:
            my_role = pos
            my_champ = cid or None
            continue
        if cid:
            if pos is not None and allies[pos] is None:
                allies[pos] = cid
            else:
                unplaced.append(cid)
    enemies = [p["championId"] for p in s.get("theirTeam", []) if p.get("championId")]
    bans = set()
    b = s.get("bans") or {}
    bans.update(c for c in b.get("myTeamBans", []) + b.get("theirTeamBans", []) if c)
    for group in s.get("actions", []):
        for a in group:
            if a.get("type") == "ban" and a.get("completed") and a.get("championId"):
                bans.add(a["championId"])
    return my_role, allies, unplaced, my_champ, enemies, sorted(bans)


def my_side(s):
    """내 진영: 0 블루, 1 레드, None 모름 (픽창 칸 번호 0~4 = 블루)."""
    me = s.get("localPlayerCellId")
    if not isinstance(me, int) or me < 0:
        return None
    return 0 if me < 5 else 1


DEMO = {
    "localPlayerCellId": 8,
    "myTeam": [
        {"cellId": 5, "championId": 516, "assignedPosition": "top"},
        {"cellId": 6, "championId": 59, "assignedPosition": "jungle"},
        {"cellId": 7, "championId": 61, "assignedPosition": "middle"},
        {"cellId": 8, "championId": 0, "assignedPosition": "bottom"},
        {"cellId": 9, "championId": 0, "assignedPosition": "utility"},
    ],
    "theirTeam": [{"championId": 238}, {"championId": 64}, {"championId": 145}, {"championId": 0}, {"championId": 0}],
    "bans": {"myTeamBans": [55, 107], "theirTeamBans": []},
    "actions": [],
}


# ---------------------------------------------------------------- 화면

def fmt_rows(m, rows):
    out = []
    for i, r in enumerate(rows, 1):
        wb = f"({m.names[r['worst_by'][1]]})" if r["worst_by"] else ""
        p = r["parts"]
        out.append((i, m.names[r["champ"]], f"{r['win'] * 100:.1f}%", f"{r['worst'] * 100:.1f}% {wb}",
                    f"{r['games']:,}", " / ".join(f"{x:+.1f}" for x in p)))
    return out


# 색 (OP.GG·lol.ps 계열 어두운 테마)
BG = "#0f1117"
PANEL = "#171a23"
PANEL2 = "#1e2230"
LINE = "#2a2f3e"
TEXT = "#e8eaf0"
SUB = "#8a90a2"
DIM = "#5c6274"
BLUE = "#4c8cf5"
RED = "#e8505f"
GREEN = "#2fbf71"
GOLD = "#e3b341"
FONT = "Malgun Gothic" if os.name == "nt" else "Noto Sans CJK KR"


def icon_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".cache")
    d = os.path.join(base, "LolDraft", "icons")
    os.makedirs(d, exist_ok=True)
    return d


class Icons:
    """챔피언 아이콘: Data Dragon에서 한 번 받아 내 컴퓨터에 저장. 못 받으면 글자 아이콘."""

    def __init__(self, root, keys):
        self.root, self.keys = root, keys
        self.dir = icon_dir()
        self.version = None
        self.photos = {}        # (cid, sub) -> PhotoImage
        self.waiting = {}       # cid -> [callback]
        self.failed = set()
        self.ctx = ssl.create_default_context()
        for ca in (os.path.join(HERE, "cacert.pem"), os.path.join(getattr(sys, "_MEIPASS", HERE), "cacert.pem")):
            if os.path.exists(ca):
                try:
                    self.ctx.load_verify_locations(cafile=ca)
                except Exception:
                    pass

    def _fetch(self, url):
        with urllib.request.urlopen(url, context=self.ctx, timeout=8) as r:
            return r.read()

    def path(self, cid):
        return os.path.join(self.dir, f"{self.keys.get(cid, cid)}.png")

    def request(self, cid, sub, cb):
        """아이콘이 준비되면 cb(PhotoImage) 호출 (메인 스레드)."""
        if cid in self.failed or cid not in self.keys:
            return
        if os.path.exists(self.path(cid)):
            cb(self._photo(cid, sub))
            return
        first = cid not in self.waiting
        self.waiting.setdefault(cid, []).append((sub, cb))
        if first:
            threading.Thread(target=self._download, args=(cid,), daemon=True).start()

    def _download(self, cid):
        ok = False
        try:
            if self.version is None:
                self.version = json.loads(self._fetch("https://ddragon.leagueoflegends.com/api/versions.json"))[0]
            data = self._fetch(f"https://ddragon.leagueoflegends.com/cdn/{self.version}/img/champion/{self.keys[cid]}.png")
            tmp = self.path(cid) + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, self.path(cid))
            ok = True
        except Exception:
            pass
        self.root.after(0, self._done, cid, ok)

    def _done(self, cid, ok):
        cbs = self.waiting.pop(cid, [])
        if not ok:
            self.failed.add(cid)
            return
        for sub, cb in cbs:
            try:
                cb(self._photo(cid, sub))
            except Exception:
                pass

    def _photo(self, cid, sub):
        import tkinter as tk
        k = (cid, sub)
        if k not in self.photos:
            self.photos[k] = tk.PhotoImage(file=self.path(cid)).subsample(sub)  # 120px → 120/sub
        return self.photos[k]


def run_gui(m, demo):
    import tkinter as tk

    root = tk.Tk()
    root.title("LoL Draft — 밴픽 추천")
    root.configure(bg=BG)
    root.geometry("940x1000")
    root.minsize(900, 640)
    root.attributes("-topmost", True)
    icons = Icons(root, m.keys)

    def F(size, bold=False):
        return (FONT, size, "bold") if bold else (FONT, size)

    def label(parent, text="", size=10, color=TEXT, bold=False, bg=None, **kw):
        return tk.Label(parent, text=text, font=F(size, bold), fg=color, bg=bg or parent["bg"], **kw)

    class Avatar(tk.Canvas):
        """챔피언 아이콘 자리 (사각형 + 글자, 아이콘이 오면 교체)."""

        def __init__(self, parent, size, sub, ring=LINE):
            super().__init__(parent, width=size, height=size, bg=parent["bg"], highlightthickness=0)
            self.size, self.sub, self.ring, self.cid, self.img = size, sub, ring, None, None
            self.set(None)

        def set(self, cid, ring=None):
            self.ring = ring or self.ring
            if cid == self.cid and cid is not None:
                self.itemconfigure("ring", outline=self.ring)
                return
            self.cid = cid
            self.delete("all")
            s = self.size
            self.create_rectangle(1, 1, s - 1, s - 1, fill=PANEL2, outline=self.ring, width=2, tags="ring")
            if cid is None:
                self.create_text(s / 2, s / 2, text="?", fill=DIM, font=F(max(8, s // 3), True))
                return
            self.create_text(s / 2, s / 2, text=m.names.get(cid, "?")[:1], fill=SUB, font=F(max(8, s // 3), True))
            icons.request(cid, self.sub, lambda img, c=cid: self._show(c, img))

        def _show(self, cid, img):
            if cid != self.cid:
                return
            self.img = img
            self.delete("all")
            self.create_image(self.size / 2, self.size / 2, image=img)
            self.create_rectangle(1, 1, self.size - 1, self.size - 1, outline=self.ring, width=2, tags="ring")

    # ---------- 머리글
    head = tk.Frame(root, bg=BG)
    head.pack(fill="x", padx=18, pady=(14, 6))
    label(head, "LoL Draft", 17, TEXT, True).pack(side="left")
    label(head, "  밴픽 추천기", 11, SUB).pack(side="left", pady=(6, 0))
    meta = m.meta
    label(head, f"패치 {' · '.join(meta['patches'])}   |   {meta['matches']:,}판 학습", 9, DIM).pack(side="right", pady=(6, 0))

    status_bar = tk.Frame(root, bg=PANEL, highlightthickness=1, highlightbackground=LINE)
    status_bar.pack(fill="x", padx=18, pady=(4, 10))
    dot = tk.Canvas(status_bar, width=12, height=12, bg=PANEL, highlightthickness=0)
    dot.pack(side="left", padx=(12, 6), pady=10)
    dot_id = dot.create_oval(2, 2, 10, 10, fill=DIM, outline="")
    status = tk.StringVar(value="시작하는 중...")
    label(status_bar, size=10, color=TEXT, bg=PANEL, textvariable=status).pack(side="left")

    # 라인 선택 + 자동/수동
    ctrl = tk.Frame(status_bar, bg=PANEL)
    ctrl.pack(side="right", padx=10)
    role_var = tk.StringVar(value="미드")
    role_btns = {}

    def pick_role(name):
        role_var.set(name)
        paint_roles()
        if not auto_var.get() or last.get("status") != "ok":
            manual()

    for name in ROLE_KO:
        b = tk.Label(ctrl, text=name, font=F(9, True), fg=SUB, bg=PANEL2, padx=10, pady=4, cursor="hand2")
        b.pack(side="left", padx=2)
        b.bind("<Button-1>", lambda e, n=name: pick_role(n))
        role_btns[name] = b

    def paint_roles():
        for n, b in role_btns.items():
            on = n == role_var.get()
            b.configure(bg=BLUE if on else PANEL2, fg="#ffffff" if on else SUB)

    auto_var = tk.BooleanVar(value=True)
    auto_btn = tk.Label(ctrl, font=F(9, True), padx=10, pady=4, cursor="hand2")
    auto_btn.pack(side="left", padx=(12, 2))

    def paint_auto():
        a = auto_var.get()
        auto_btn.configure(text="자동 읽기 ON" if a else "자동 읽기 OFF", bg=GREEN if a else PANEL2,
                           fg="#0b1a12" if a else SUB)

    def toggle_auto(_=None):
        auto_var.set(not auto_var.get())
        paint_auto()
        show_manual(not auto_var.get())

    auto_btn.bind("<Button-1>", toggle_auto)

    # ---------- 수동 입력 (자동 읽기 OFF일 때 보임)
    manual_box = tk.Frame(root, bg=PANEL, highlightthickness=1, highlightbackground=LINE)
    ally_var, enemy_var, ban_var = tk.StringVar(value="- - - - -"), tk.StringVar(), tk.StringVar()
    for i, (t, v, hint) in enumerate((("우리 팀", ally_var, "탑 정글 미드 원딜 서폿 순서, 모르면 -"),
                                      ("상대 픽", enemy_var, "보이는 챔피언만, 순서 상관없음"),
                                      ("밴", ban_var, ""))):
        label(manual_box, t, 9, SUB, True).grid(row=i, column=0, sticky="w", padx=(12, 8), pady=4)
        e = tk.Entry(manual_box, textvariable=v, font=F(10), bg=PANEL2, fg=TEXT, insertbackground=TEXT,
                     relief="flat", highlightthickness=1, highlightbackground=LINE, highlightcolor=BLUE, width=52)
        e.grid(row=i, column=1, sticky="we", pady=4, ipady=3)
        e.bind("<Return>", lambda ev: manual())
        label(manual_box, hint, 8, DIM).grid(row=i, column=2, sticky="w", padx=8)
    go = tk.Label(manual_box, text="추천 받기", font=F(10, True), bg=BLUE, fg="#ffffff", padx=16, pady=6, cursor="hand2")
    go.grid(row=0, column=3, rowspan=3, padx=12)
    go.bind("<Button-1>", lambda e: manual())
    err = tk.StringVar()
    label(manual_box, "", 9, RED, textvariable=err).grid(row=3, column=0, columnspan=4, sticky="w", padx=12, pady=(0, 6))

    body = tk.Frame(root, bg=BG)

    def show_manual(on):
        if on:
            manual_box.pack(fill="x", padx=18, pady=(0, 10), before=body)
        else:
            manual_box.pack_forget()

    body.pack(fill="both", expand=True, padx=18)

    # ---------- 예상 승률 (우리 팀 vs 상대 팀)
    match = tk.Frame(body, bg=PANEL, highlightthickness=1, highlightbackground=LINE)
    match.pack(fill="x", pady=(0, 10))
    mrow = tk.Frame(match, bg=PANEL)
    mrow.pack(fill="x", padx=14, pady=(10, 2))
    label(mrow, "우리 팀", 10, BLUE, True).pack(side="left")
    us_pct = label(mrow, "—", 20, BLUE, True)
    us_pct.pack(side="left", padx=(8, 0))
    label(mrow, "상대 팀", 10, RED, True).pack(side="right")
    them_pct = label(mrow, "—", 20, RED, True)
    them_pct.pack(side="right", padx=(0, 8))
    mid = label(mrow, "예상 승률", 9, SUB, True)
    mid.pack(expand=True)
    split = tk.Canvas(match, height=8, bg=PANEL, highlightthickness=0)
    split.pack(fill="x", padx=14)
    match_sub = label(match, "", 8, DIM)
    match_sub.pack(fill="x", padx=14, pady=(4, 8))

    def draw_match(p, sub):
        split.delete("all")
        w = max(split.winfo_width(), 200)
        if p is None:
            us_pct.configure(text="—")
            them_pct.configure(text="—")
            split.create_rectangle(0, 0, w, 8, fill=PANEL2, outline="")
        else:
            us_pct.configure(text=f"{p * 100:.1f}%")
            them_pct.configure(text=f"{(1 - p) * 100:.1f}%")
            x = w * p
            split.create_rectangle(0, 0, x, 8, fill=BLUE, outline="")
            split.create_rectangle(x, 0, w, 8, fill=RED, outline="")
            split.create_line(w / 2, 0, w / 2, 8, fill=BG)
        match_sub.configure(text=sub)

    # ---------- 양 팀 보드
    board = tk.Frame(body, bg=BG)
    board.pack(fill="x")
    board.columnconfigure(0, weight=1, uniform="t")
    board.columnconfigure(1, weight=1, uniform="t")

    def team_panel(col, title, color):
        p = tk.Frame(board, bg=PANEL, highlightthickness=1, highlightbackground=LINE)
        p.grid(row=0, column=col, sticky="nsew", padx=(0, 6) if col == 0 else (6, 0))
        tk.Frame(p, bg=color, height=3).pack(fill="x")
        hd = tk.Frame(p, bg=PANEL)
        hd.pack(fill="x", padx=12, pady=(8, 4))
        label(hd, title, 11, color, True).pack(side="left")
        note = label(hd, "", 8, DIM)
        note.pack(side="right")
        rows = []
        for r in range(5):
            f = tk.Frame(p, bg=PANEL)
            f.pack(fill="x", padx=12, pady=1)
            rl = label(f, ROLE_KO[r], 9, SUB, True, width=4, anchor="w")
            rl.pack(side="left")
            av = Avatar(f, 26, 5)
            av.pack(side="left", padx=(0, 10))
            nm = label(f, "—", 10, DIM)
            nm.pack(side="left")
            tag = label(f, "", 8, GOLD, True)
            tag.pack(side="right")
            rows.append((f, rl, av, nm, tag))
        tk.Frame(p, bg=PANEL, height=8).pack()
        return rows, note

    ally_rows, _ = team_panel(0, "우리 팀", BLUE)
    enemy_rows, enemy_note = team_panel(1, "상대 팀", RED)

    ban_bar = tk.Frame(body, bg=BG)
    ban_bar.pack(fill="x", pady=(6, 8))
    label(ban_bar, "밴", 9, SUB, True).pack(side="left", padx=(2, 8))
    ban_icons = []
    for _ in range(10):
        a = Avatar(ban_bar, 24, 5)
        a.pack(side="left", padx=2)
        ban_icons.append(a)

    # ---------- 추천 목록
    rec = tk.Frame(body, bg=PANEL, highlightthickness=1, highlightbackground=LINE)
    rec.pack(fill="both", expand=True)
    rh = tk.Frame(rec, bg=PANEL)
    rh.pack(fill="x", padx=14, pady=(10, 4))
    rec_title = label(rh, "추천", 12, TEXT, True)
    rec_title.pack(side="left")
    rec_sub = label(rh, "", 9, SUB)
    rec_sub.pack(side="left", padx=10)

    COLS = (36, 196, 190, 180, 200)   # 순위, 챔피언, 예상 승률, 카운터 당하면, 기여 (픽셀)
    RH = 40

    def col(parent, w, h=RH):
        c = tk.Frame(parent, bg=parent["bg"], width=w, height=h)
        c.pack(side="left")
        c.pack_propagate(False)
        return c

    hdr = tk.Frame(rec, bg=PANEL)
    hdr.pack(fill="x", padx=14)
    for t, w in zip(("#", "챔피언", "예상 승률", "카운터 당하면", "기여 %p  챔피언 · 시너지 · 상성 · 조합"), COLS):
        label(col(hdr, w, 20), t, 8, DIM, True, anchor="w").pack(anchor="w")
    tk.Frame(rec, bg=LINE, height=1).pack(fill="x", padx=14, pady=(4, 0))

    rec_rows = []
    for i in range(10):
        f = tk.Frame(rec, bg=PANEL)
        f.pack(fill="x", padx=14)
        rank = label(col(f, COLS[0]), str(i + 1), 11, SUB, True, anchor="w")
        rank.pack(anchor="w", pady=10)
        cell = col(f, COLS[1])
        av = Avatar(cell, 32, 4)
        av.pack(side="left", pady=5)
        nb = tk.Frame(cell, bg=PANEL)
        nb.pack(side="left", padx=10)
        nm = label(nb, "", 10, TEXT, True, anchor="w")
        nm.pack(anchor="w")
        gm = label(nb, "", 8, DIM, anchor="w")
        gm.pack(anchor="w")
        wc = col(f, COLS[2])
        win = label(wc, "", 12, TEXT, True, anchor="w")
        win.pack(anchor="w", pady=(3, 0))
        bar = tk.Canvas(wc, width=150, height=5, bg=PANEL, highlightthickness=0)
        bar.pack(anchor="w")
        cc = col(f, COLS[3])
        worst = label(cc, "", 10, TEXT, False, anchor="w")
        worst.pack(anchor="w", pady=(3, 0))
        wby = label(cc, "", 8, DIM, anchor="w")
        wby.pack(anchor="w")
        chips = col(f, COLS[4])
        chip_l = []
        for _ in range(4):
            c = label(chips, "", 9, SUB, True, width=5, anchor="w")
            c.pack(side="left", pady=10)
            chip_l.append(c)
        line = tk.Frame(rec, bg=PANEL2, height=1)
        line.pack(fill="x", padx=14)
        rec_rows.append(dict(frame=f, line=line, rank=rank, av=av, nm=nm, gm=gm, win=win, bar=bar,
                             worst=worst, wby=wby, chips=chip_l))

    empty_msg = label(rec, "", 10, SUB)
    empty_msg.pack(pady=8)

    # ---------- 아래: 우리 팀 요약 + 안내
    foot = tk.Frame(root, bg=BG)
    foot.pack(fill="x", padx=18, pady=(8, 12), side="bottom", before=body)
    summary = tk.Frame(foot, bg=BG)
    summary.pack(fill="x")
    warn_var = tk.StringVar()
    warn_lbl = label(foot, "", 9, RED, True, textvariable=warn_var, anchor="w", justify="left")
    note_lbl = label(foot, "조합만 보고 낸 확률이에요. 1~2%p 차이면 손에 익은 챔피언이 더 나아요.", 8, DIM, anchor="w")
    note_lbl.pack(fill="x", pady=(4, 0))

    def win_color(p):
        if p >= 0.53:
            return BLUE
        if p <= 0.47:
            return RED
        return TEXT

    def chip_color(v):
        return GREEN if v >= 0.3 else RED if v <= -0.3 else SUB

    def draw_rows(rows, my_role):
        for i, w in enumerate(rec_rows):
            if i >= len(rows):
                w["frame"].pack_forget()
                w["line"].pack_forget()
                continue
            if not w["frame"].winfo_ismapped():
                w["frame"].pack(fill="x", padx=14)
                w["line"].pack(fill="x", padx=14)
            r = rows[i]
            top = i == 0
            w["rank"].configure(fg=GOLD if top else SUB)
            w["av"].set(r["champ"], ring=GOLD if top else LINE)
            w["nm"].configure(text=m.names.get(r["champ"], r["champ"]))
            w["gm"].configure(text=f"{ROLE_KO[my_role]} {r['games']:,}판")
            w["win"].configure(text=f"{r['win'] * 100:.1f}%", fg=win_color(r["win"]))
            b = w["bar"]
            b.delete("all")
            b.create_rectangle(0, 0, 150, 5, fill=PANEL2, outline="")
            frac = min(1.0, max(0.02, (r["win"] - 0.40) / 0.20))
            b.create_rectangle(0, 0, 150 * frac, 5, fill=win_color(r["win"]) if r["win"] != 0.5 else SUB, outline="")
            b.create_line(75, 0, 75, 5, fill=BG)
            w["worst"].configure(text=f"{r['worst'] * 100:.1f}%", fg=win_color(r["worst"]))
            wb = r["worst_by"]
            w["wby"].configure(text=f"vs {ROLE_KO[wb[0]]} {m.names[wb[1]]}" if wb else "")
            for c, v in zip(w["chips"], r["parts"]):
                c.configure(text=f"{v:+.1f}", fg=chip_color(v))
        empty_msg.configure(text="" if rows else "추천할 챔피언이 없어요 (판수 부족 또는 모두 픽·밴됨)")
        if rows:
            empty_msg.pack_forget()
        elif not empty_msg.winfo_ismapped():
            empty_msg.pack(pady=8)

    def draw_board(my_role, allies, enemies, bans, my_champ=None):
        for r, (f, rl, av, nm, tag) in enumerate(ally_rows):
            c = allies[r]
            mine = r == my_role
            if mine and my_champ:
                c = my_champ
            av.set(c, ring=GOLD if mine else BLUE if c else LINE)
            nm.configure(text=m.names.get(c, "—") if c else ("내 차례" if mine else "—"),
                         fg=GOLD if mine else TEXT if c else DIM)
            tag.configure(text="ME" if mine else "")
            rl.configure(fg=GOLD if mine else SUB)
        for r, (f, rl, av, nm, tag) in enumerate(enemy_rows):
            c = enemies[r]
            av.set(c, ring=RED if c else LINE)
            nm.configure(text=m.names.get(c, "—") if c else "—", fg=TEXT if c else DIM)
        enemy_note.configure(text="라인은 픽률로 추정" if any(enemies) else "")
        for i, a in enumerate(ban_icons):
            c = bans[i] if i < len(bans) else None
            if c is None:
                a.pack_forget()
            else:
                if not a.winfo_ismapped():
                    a.pack(side="left", padx=2)
                a.set(c, ring=LINE)

    def draw_summary(team_text):
        for w in summary.winfo_children():
            w.destroy()
        warn_var.set("")
        warn_lbl.pack_forget()
        if not team_text:
            return
        main, _, warn = team_text.partition(" | 주의: ")
        label(summary, "1순위로 고르면 우리 팀", 9, SUB, True).pack(side="left", padx=(0, 8))
        for part in main.split(", "):
            tk.Label(summary, text=part, font=F(9, True), fg=TEXT, bg=PANEL2, padx=8, pady=3).pack(side="left", padx=2)
        if warn:
            warn_var.set("주의: " + warn)
            warn_lbl.pack(fill="x", pady=(4, 0), before=note_lbl)
        else:
            warn_lbl.pack_forget()

    state = {"key": None, "busy": False}

    def show(key, rows, team_text, my_role, allies, enemies, bans, my_champ, head, cur, msub):
        if key != state["key"]:
            return
        draw_match(cur, msub)
        rec_title.configure(text=f"{ROLE_KO[my_role]} 추천")
        rec_sub.configure(text=head)
        draw_board(my_role, allies, enemies, bans, my_champ)
        draw_rows(rows, my_role)
        draw_summary(team_text)

    def compute(key, my_role, allies, enemy_ids, bans, head, my_champ=None, side=None):
        def work():
            try:
                enemies = m.assign_roles(enemy_ids[:5], [None] * 5)
                rows = m.recommend(my_role, allies, enemies, bans)
                if side is not None:          # 진영 이점도 후보 승률에 똑같이 반영 (순위는 그대로)
                    d = m.intercept if side == 0 else -m.intercept
                    lg = lambda p: math.log(p / (1 - p))
                    for r in rows:
                        r["win"] = 1 / (1 + math.exp(-(lg(r["win"]) + d)))
                        r["worst"] = 1 / (1 + math.exp(-(lg(r["worst"]) + d)))
                team_text = ""
                if rows:
                    best = list(allies)
                    best[my_role] = rows[0]["champ"]
                    team_text = m.team_notes(best)
                # 지금 조합의 예상 승률 (빈 칸은 픽률 평균, 진영을 알면 진영 이점 포함)
                us = list(allies)
                us[my_role] = my_champ
                taken = {c for c in us + list(enemies) + list(bans) if c}
                z = m.expected(us, enemies, taken)
                if side is not None:
                    z += m.intercept if side == 0 else -m.intercept
                cur = 1 / (1 + math.exp(-z))
                known = sum(1 for c in us if c) + sum(1 for c in enemies if c)
                parts = [f"지금 조합 기준 ({known}/10명 확정, 나머지는 픽률 평균)"]
                parts.append({0: "블루 진영", 1: "레드 진영"}.get(side, "진영 모름"))
                if rows and not my_champ:
                    parts.append(f"1순위 {m.names.get(rows[0]['champ'], '')} 고르면 {rows[0]['win'] * 100:.1f}%")
                msub = "  ·  ".join(parts)
                root.after(0, show, key, rows, team_text, my_role, allies, enemies, list(bans), my_champ, head, cur, msub)
            finally:
                state["busy"] = False
        state["busy"] = True
        state["key"] = key
        threading.Thread(target=work, daemon=True).start()

    def parse_ids(text, allow_empty):
        out, errs = [], []
        for tok in text.replace(",", " ").split():
            if allow_empty and tok in EMPTY:
                out.append(None)
                continue
            cid, e = m.find(tok)
            if e:
                errs.append(e)
            out.append(cid)
        return out, errs

    def manual():
        my_role = ROLE_ALIASES.get(norm(role_var.get()), 2)
        allies, e1 = parse_ids(ally_var.get(), True)
        enemies, e2 = parse_ids(enemy_var.get(), False)
        bans, e3 = parse_ids(ban_var.get(), False)
        errs = e1 + e2 + e3
        err.set("  ".join(errs))
        if errs:
            return
        allies = (allies + [None] * 5)[:5]
        allies[my_role] = None
        enemies = [c for c in enemies if c]
        key = ("manual", my_role, tuple(allies), tuple(enemies), tuple(bans), time.time())
        compute(key, my_role, allies, enemies, bans, "수동 입력")

    client = Client()
    last = {"session": None, "status": None}

    def poll():
        while True:
            if auto_var.get():
                if demo:
                    s, st = DEMO, "ok"
                else:
                    s, st = client.get("/lol-champ-select/v1/session")
                last["session"], last["status"] = s, st
                if s is None and not st.startswith("픽창 대기"):
                    time.sleep(4)                 # 클라이언트가 꺼져 있으면 5초마다만 찾기
            time.sleep(1)

    def set_status(text, color):
        status.set(text)
        dot.itemconfigure(dot_id, fill=color)

    def names(ids):
        return " ".join(m.names.get(c, str(c)).replace(" ", "") for c in ids)

    def tick():
        s, st = last["session"], last["status"]
        if auto_var.get() and st:
            if s is None:
                set_status(st, GOLD if st.startswith("픽창 대기") else DIM)
            else:
                my_role, allies, unplaced, my_champ, enemy_ids, bans = parse_session(s)
                if my_role is None:
                    my_role = ROLE_ALIASES.get(norm(role_var.get()), 2)
                    set_status("픽창 읽는 중 — 라인 정보가 없어요. 오른쪽에서 내 라인을 고르세요", GOLD)
                else:
                    set_status("픽창 자동 읽는 중" + (" (데모)" if demo else ""), GREEN)
                    role_var.set(ROLE_KO[my_role])
                    paint_roles()
                allies[my_role] = None
                for c in unplaced:                       # 라인 모르는 아군은 빈 칸에 픽률 기준으로
                    free = [r for r in range(5) if allies[r] is None and r != my_role]
                    if free:
                        allies[max(free, key=lambda r: m.games.get(r, {}).get(c, 0))] = c
                ally_var.set(" ".join(m.names[c].replace(" ", "") if c else "-" for c in allies))
                enemy_var.set(names(enemy_ids))
                ban_var.set(names(bans))
                key = ("auto", my_role, tuple(allies), tuple(enemy_ids), tuple(bans), my_champ, my_side(s))
                if key != state["key"] and not state["busy"]:
                    head = f"지금 고른 챔피언: {m.names.get(my_champ, my_champ)}" if my_champ else "픽창 기준"
                    compute(key, my_role, allies, enemy_ids, bans, head, my_champ, my_side(s))
        elif not auto_var.get():
            set_status("수동 모드 — 아래 칸에 입력하고 '추천 받기'", BLUE)
        root.after(500, tick)

    paint_roles()
    paint_auto()
    draw_board(2, [None] * 5, [None] * 5, [])
    draw_rows([], 2)
    empty_msg.configure(text="픽창에 들어가면 자동으로 추천이 떠요")
    threading.Thread(target=poll, daemon=True).start()
    root.after(300, tick)
    if not demo:
        root.after(1500, lambda: (last["status"] != "ok" and state["key"] is None) and manual())
    root.mainloop()


def run_console(m, demo):
    """tkinter가 없을 때: 픽창이 바뀔 때마다 콘솔에 추천을 다시 출력."""
    client = Client()
    prev = None
    print("픽창을 기다리는 중... (끄려면 창을 닫으세요)")
    while True:
        s, st = (DEMO, "ok") if demo else client.get("/lol-champ-select/v1/session")
        if s is None:
            if st != prev:
                print(f"● {st}")
                prev = st
            time.sleep(2)
            continue
        my_role, allies, unplaced, my_champ, enemy_ids, bans = parse_session(s)
        my_role = 2 if my_role is None else my_role
        allies[my_role] = None
        key = (my_role, tuple(allies), tuple(enemy_ids), tuple(bans))
        if key != prev:
            prev = key
            enemies = m.assign_roles(enemy_ids[:5], [None] * 5)
            rows = m.recommend(my_role, allies, enemies, bans)
            print(f"\n[{ROLE_KO[my_role]} 추천] 상대: {', '.join(m.names[c] for c in enemy_ids) or '없음'}")
            for r in fmt_rows(m, rows):
                print("  {:>2}  {:<10} {:>7} {:<18} {:>7}판  {}".format(*r))
        if demo:
            return
        time.sleep(1)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    # exe로 묶었으면: exe 옆에 model.json이 있으면 그걸, 없으면 exe 안에 든 걸 씀
    places = []
    if getattr(sys, "frozen", False):
        places.append(os.path.dirname(sys.executable))
        places.append(getattr(sys, "_MEIPASS", HERE))
    places.append(HERE)
    path = next((os.path.join(d, "model.json") for d in places if os.path.exists(os.path.join(d, "model.json"))), None)
    if not path:
        print("model.json이 없어요. 먼저 모델학습.bat을 실행하세요.")
        return
    m = Model(path)
    demo = "--demo" in sys.argv
    try:
        import tkinter  # noqa: F401
    except ImportError:
        return run_console(m, demo)
    if "--console" in sys.argv:
        return run_console(m, demo)
    run_gui(m, demo)


if __name__ == "__main__":
    main()
