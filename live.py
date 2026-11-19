"""자동 추천 창: 롤 클라이언트 픽창을 읽어서 추천을 자동으로 갱신 (설치할 패키지 없음)

- 롤 클라이언트가 켜져 있으면 픽창(내 라인, 우리 팀, 상대 픽, 밴)을 1초마다 읽어요.
  클라이언트가 내 컴퓨터 안에서 여는 통로(LCU API)를 읽기만 하고, 클라이언트를 조작하지 않아요.
- 클라이언트가 없거나 픽창이 아니면 창에 직접 입력해서 쓸 수 있어요(수동 모드).

실행: 자동추천.bat 더블클릭, 또는  python live.py
시험: python live.py --demo   (가짜 픽창으로 화면 확인)
"""
import base64
import json
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


def run_gui(m, demo):
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title(f"롤 밴픽 자동 추천 — 패치 {', '.join(m.meta['patches'])}, {m.meta['matches']:,}판 학습")
    root.geometry("860x560")
    root.attributes("-topmost", True)

    status = tk.StringVar(value="시작하는 중...")
    tk.Label(root, textvariable=status, anchor="w", font=("맑은 고딕", 10, "bold")).pack(fill="x", padx=8, pady=(8, 2))

    # 입력 칸 (픽창이면 자동으로 채워지고, 아니면 직접 입력)
    form = tk.Frame(root)
    form.pack(fill="x", padx=8)
    tk.Label(form, text="내 라인").grid(row=0, column=0, sticky="w")
    role_var = tk.StringVar(value="미드")
    ttk.Combobox(form, textvariable=role_var, values=ROLE_KO, width=6, state="readonly").grid(row=0, column=1, sticky="w")
    tk.Label(form, text="우리 팀 (탑 정글 미드 원딜 서폿, 모르면 -)").grid(row=1, column=0, columnspan=2, sticky="w")
    ally_var = tk.StringVar(value="- - - - -")
    tk.Entry(form, textvariable=ally_var, width=48).grid(row=1, column=2, sticky="w")
    tk.Label(form, text="상대 픽 (보이는 것만)").grid(row=2, column=0, columnspan=2, sticky="w")
    enemy_var = tk.StringVar()
    tk.Entry(form, textvariable=enemy_var, width=48).grid(row=2, column=2, sticky="w")
    tk.Label(form, text="밴").grid(row=3, column=0, columnspan=2, sticky="w")
    ban_var = tk.StringVar()
    tk.Entry(form, textvariable=ban_var, width=48).grid(row=3, column=2, sticky="w")
    auto_var = tk.BooleanVar(value=True)
    tk.Checkbutton(form, text="픽창 자동 읽기", variable=auto_var).grid(row=0, column=2, sticky="w")
    btn = tk.Button(form, text="추천 (수동)", width=12)
    btn.grid(row=0, column=3, rowspan=2, padx=8)
    err = tk.StringVar()
    tk.Label(root, textvariable=err, fg="#c0392b", anchor="w").pack(fill="x", padx=8)

    cols = ("순위", "챔피언", "예상 승률", "카운터 당하면", "데이터(판)", "기여 챔피언/시너지/상성/조합 %p")
    tree = ttk.Treeview(root, columns=cols, show="headings", height=10)
    for c, w in zip(cols, (40, 110, 80, 160, 80, 260)):
        tree.heading(c, text=c)
        tree.column(c, width=w, anchor="center")
    tree.pack(fill="both", expand=True, padx=8, pady=4)
    notes = tk.StringVar()
    tk.Label(root, textvariable=notes, anchor="w", justify="left", wraplength=830).pack(fill="x", padx=8)
    tk.Label(root, text="※ 조합만 본 확률이에요. 1~2%p 차이면 손에 익은 챔피언이 더 나아요.",
             fg="#666", anchor="w").pack(fill="x", padx=8, pady=(0, 8))

    state = {"key": None, "busy": False}

    def names(ids):
        return " ".join(m.names.get(c, str(c)).replace(" ", "") for c in ids)

    def show(key, rows, team_text, head):
        if key != state["key"]:
            return
        tree.delete(*tree.get_children())
        for r in fmt_rows(m, rows):
            tree.insert("", "end", values=r)
        notes.set(head + ("\n1순위로 고르면 우리 팀: " + team_text if team_text else ""))

    def compute(key, my_role, allies, enemy_ids, bans, head):
        def work():
            try:
                enemies = m.assign_roles(enemy_ids[:5], [None] * 5)
                rows = m.recommend(my_role, allies, enemies, bans)
                team_text = ""
                if rows:
                    best = list(allies)
                    best[my_role] = rows[0]["champ"]
                    team_text = m.team_notes(best)
                guess = ", ".join(f"{ROLE_KO[r]} {m.names[c]}" for r, c in enumerate(enemies) if c)
                h = head + (f"  |  상대 라인 추정: {guess}" if guess else "")
                root.after(0, show, key, rows, team_text, h)
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
        compute(key, my_role, allies, enemies, bans, f"[수동] {ROLE_KO[my_role]} 추천")

    btn.configure(command=manual)

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

    def tick():
        s, st = last["session"], last["status"]
        if auto_var.get() and st:
            if s is None:
                status.set(f"● {st}")
            else:
                my_role, allies, unplaced, my_champ, enemy_ids, bans = parse_session(s)
                if my_role is None:
                    my_role = ROLE_ALIASES.get(norm(role_var.get()), 2)
                    status.set("● 픽창 (라인 정보 없음 — 위에서 내 라인을 고르세요)")
                else:
                    status.set("● 픽창 자동 읽는 중" + (" (데모)" if demo else ""))
                    role_var.set(ROLE_KO[my_role])
                allies[my_role] = None
                for c in unplaced:                       # 라인 모르는 아군은 빈 칸에 픽률 기준으로
                    free = [r for r in range(5) if allies[r] is None and r != my_role]
                    if free:
                        allies[max(free, key=lambda r: m.games.get(r, {}).get(c, 0))] = c
                ally_var.set(" ".join(m.names[c].replace(" ", "") if c else "-" for c in allies))
                enemy_var.set(names(enemy_ids))
                ban_var.set(names(bans))
                key = ("auto", my_role, tuple(allies), tuple(enemy_ids), tuple(bans))
                if key != state["key"] and not state["busy"]:
                    head = f"[자동] {ROLE_KO[my_role]} 추천"
                    if my_champ:
                        head += f"  (지금 고른 챔피언: {m.names.get(my_champ, my_champ)})"
                    compute(key, my_role, allies, enemy_ids, bans, head)
        elif not auto_var.get():
            status.set("● 수동 모드: 칸에 입력하고 '추천' 버튼")
        root.after(500, tick)

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
