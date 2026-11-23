"""롤 밴픽 추천기 (설치할 패키지 없음, model.json만 있으면 동작)

실행: 추천받기.bat 더블클릭, 또는  python recommend.py
"""
import bisect
import itertools
import json
import math
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROLE_KO = ["탑", "정글", "미드", "원딜", "서폿"]
ROLE_ALIASES = {
    "탑": 0, "top": 0,
    "정글": 1, "정": 1, "jg": 1, "jungle": 1,
    "미드": 2, "미": 2, "mid": 2,
    "원딜": 3, "바텀": 3, "봇": 3, "ad": 3, "adc": 3, "bot": 3,
    "서폿": 4, "서포터": 4, "서폿터": 4, "sup": 4, "support": 4,
}
EMPTY = {"-", "?", "x", "_", "없음", "모름"}
MIN_GAMES = 500       # 그 라인에서 이 판수 미만인 챔피언은 추천하지 않음 (model_config.json의 "min_games"로 변경)
COUNTER_SHARE = 0.01  # '카운터 당하면' 계산에 쓰는 상대 후보: 그 라인 픽률 1% 이상인 챔피언
TEAM_SAMPLES = 40     # 모르는 칸이 있을 때 팀 조합 점수를 평균 내는 표본 수


def sigmoid(x):
    return 1 / (1 + math.exp(-x))


def norm(s):
    return "".join(s.split()).lower().replace("'", "").replace(".", "")


class Model:
    def __init__(self, path=os.path.join(HERE, "model.json")):
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
        self.meta = m["meta"]
        self.champ = {(int(r), int(c)): w for r, d in m["champ"].items() for c, w in d.items()}
        self.lane = {(r, a, b): w for r, a, b, w in m["lane"]}
        self.cross = {(i, j, a, b): w for i, j, a, b, w in m["cross"]}
        self.duo = {(i, j, a, b): w for i, j, a, b, w in m["duo"]}
        self.games = {int(r): {int(c): n for c, n in d.items()} for r, d in m["role_games"].items()}
        self.role_total = {r: sum(d.values()) for r, d in self.games.items()}
        self.names = {int(k): v for k, v in m["names"].items()}
        self.min_games = MIN_GAMES
        cfg_path = os.path.join(os.path.dirname(path), "model_config.json")
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path, encoding="utf-8") as f:
                    self.min_games = int(json.load(f).get("min_games", MIN_GAMES))
            except (ValueError, OSError):
                pass
        self.team = m.get("team")
        if self.team:
            t = self.team
            self.attr = {int(c): dict(zip(t["attr_keys"], v)) for c, v in t["attr"].items()}
            parse = lambda d: {tuple(map(int, k.split(","))): v for k, v in d.items()}
            self.magic, self.amt, self.scaling = parse(t["magic"]), parse(t["amt"]), parse(t["scaling"])
        self.keys = {int(k): v for k, v in m.get("keys", {}).items()}  # 영문 키 (아이콘 파일 이름)
        self.lookup = {}
        for cid, name in self.names.items():
            self.lookup[norm(name)] = cid
            self.lookup[norm(m["keys"][str(cid)])] = cid

    # ---------- 이름 찾기
    def find(self, text):
        t = norm(text)
        if t in self.lookup:
            return self.lookup[t], None
        hits = sorted({cid for k, cid in self.lookup.items() if t and t in k})
        if len(hits) == 1:
            return hits[0], None
        if not hits:
            return None, f"'{text}' 챔피언을 못 찾았어요."
        return None, f"'{text}'가 여러 챔피언과 겹쳐요: " + ", ".join(self.names[h] for h in hits[:8])

    # ---------- 점수 (우리 팀 기준 로짓, 진영 효과 제외)
    def pair_terms(self, us, them):
        """us/them: 라인별 챔피언 id 리스트(5칸, 모르면 None). 둘 다 정해진 칸만 계산."""
        s = 0.0
        for r in range(5):
            if us[r] is not None:
                s += self.champ.get((r, us[r]), 0.0)
            if them[r] is not None:
                s -= self.champ.get((r, them[r]), 0.0)
        for r in range(5):
            a, b = us[r], them[r]
            if a is not None and b is not None:
                s += self.lane.get((r, a, b), 0.0) if a < b else -self.lane.get((r, b, a), 0.0)
        for i in range(5):
            for j in range(5):
                if i == j or us[i] is None or them[j] is None:
                    continue
                if (i, us[i]) < (j, them[j]):
                    s += self.cross.get((i, j, us[i], them[j]), 0.0)
                else:
                    s -= self.cross.get((j, i, them[j], us[i]), 0.0)
        for i in range(5):
            for j in range(i + 1, 5):
                if us[i] is not None and us[j] is not None:
                    s += self.duo.get((i, j, us[i], us[j]), 0.0)
                if them[i] is not None and them[j] is not None:
                    s -= self.duo.get((i, j, them[i], them[j]), 0.0)
        return s

    def pool(self, role, taken, min_share):
        d = self.games.get(role, {})
        tot = self.role_total.get(role, 1)
        cands = {c: n for c, n in d.items() if c not in taken and n / tot >= min_share}
        z = sum(cands.values()) or 1
        return {c: n / z for c, n in cands.items()}

    def expected_pairs(self, us, them, taken):
        """모르는 칸은 그 라인 픽률대로 나올 거라고 보고 평균 낸 점수 (챔피언·상성·듀오)."""
        base = self.pair_terms(us, them)
        for side, team in (("us", us), ("them", them)):
            for r in range(5):
                if team[r] is not None:
                    continue
                dist = self.pool(r, taken, 0.002)
                exp = 0.0
                for c, p in dist.items():
                    t = list(team)
                    t[r] = c
                    full = self.pair_terms(t, them) if side == "us" else self.pair_terms(us, t)
                    exp += p * (full - base)
                base += exp
        return base

    # ---------- 팀 조합 (AD/AP, 앞라인, CC, 이니시 ...)
    def profile(self, team):
        A = [self.attr.get(c, {}) for c in team]
        get = lambda k: [a.get(k, 0.0) for a in A]
        mags = [self.magic.get((r, c), 0.4) for r, c in enumerate(team)]
        amts = [self.amt.get((r, c), 15000.0) for r, c in enumerate(team)]
        tough, tank = get("tough"), get("tank")
        return {
            "magic_share": sum(m * a for m, a in zip(mags, amts)) / sum(amts),
            "tough": sum(tough), "cc": sum(get("cc")), "mob": sum(get("mob")), "util": sum(get("util")),
            "dmg": sum(get("dmg")), "engage": sum(get("engage")), "peel": sum(get("peel")),
            "poke": sum(get("poke")), "split": sum(get("split")), "fightmage": sum(get("fightmage")),
            "pick": sum(get("pick")),
            "frontline": float(sum(1 for x, t in zip(tough, tank) if x >= 3 or t > 0)),
            "scaling": sum(self.scaling.get((r, c), 0.0) for r, c in enumerate(team)),
        }

    def team_score(self, us, them):
        t = self.team
        pu, pt = self.profile(us), self.profile(them)
        f = [pu[k] - pt[k] for k in t["linear"]]
        for k, edges in t["bins"].items():
            v = [0.0] * (len(edges) + 1)
            v[bisect.bisect_right(edges, pu[k])] += 1
            v[bisect.bisect_right(edges, pt[k])] -= 1
            f.extend(v)
        for a, b in t["cross"]:
            f.append(pu[a] * pt[b] - pt[a] * pu[b])
        return sum(w * x for w, x in zip(t["weights"], f))

    def expected_team(self, us, them, taken, seed=7):
        if not self.team:
            return 0.0
        holes = [(side, r) for side, team in (("us", us), ("them", them)) for r in range(5) if team[r] is None]
        if not holes:
            return self.team_score(us, them)
        rng = random.Random(seed)  # 후보마다 같은 표본을 써서 비교가 공정하게
        pools = {r: self.pool(r, taken, 0.002) for _, r in holes}
        total = 0.0
        for _ in range(TEAM_SAMPLES):
            u, t, used = list(us), list(them), set(taken)
            for side, r in holes:
                items = [(c, p) for c, p in pools[r].items() if c not in used] or list(pools[r].items())
                c = rng.choices([c for c, _ in items], [p for _, p in items])[0]
                used.add(c)
                (u if side == "us" else t)[r] = c
            total += self.team_score(u, t)
        return total / TEAM_SAMPLES

    def expected(self, us, them, taken):
        return self.expected_pairs(us, them, taken) + self.expected_team(us, them, taken)

    def interaction(self, role, c, j, e):
        """우리 role 라인 챔피언 c 와 상대 j 라인 챔피언 e 사이의 상성 점수 (우리 기준)."""
        if j == role:
            return self.lane.get((role, c, e), 0.0) if c < e else -self.lane.get((role, e, c), 0.0)
        if (role, c) < (j, e):
            return self.cross.get((role, j, c, e), 0.0)
        return -self.cross.get((j, role, e, c), 0.0)

    def breakdown(self, us, them, role):
        """추천 챔피언 한 명이 승률에 주는 영향을 나눠서 (%p, 대략)."""
        c = us[role]
        champ = self.champ.get((role, c), 0.0)
        syn = 0.0
        for j in range(5):
            if j == role or us[j] is None:
                continue
            i, k = (role, j) if role < j else (j, role)
            syn += self.duo.get((i, k, us[i], us[k]), 0.0)
        vs = sum(self.interaction(role, c, j, them[j]) for j in range(5) if them[j] is not None)
        return [round(25 * x, 1) for x in (champ, syn, vs)]

    def team_notes(self, team):
        """정해진 챔피언만으로 본 우리 팀 조합 요약과 경고."""
        known = [(r, c) for r, c in enumerate(team) if c is not None]
        if not known or not self.team:
            return ""
        mags = [self.magic.get((r, c), 0.4) for r, c in known]
        amts = [self.amt.get((r, c), 15000.0) for r, c in known]
        magic = sum(m * a for m, a in zip(mags, amts)) / sum(amts)
        A = [self.attr.get(c, {}) for _, c in known]
        front = sum(1 for a in A if a.get("tough", 0) >= 3 or a.get("tank", 0) > 0)
        engage = sum(1 for a in A if a.get("engage", 0) > 0)
        cc = sum(a.get("cc", 0) for a in A)
        notes = [f"마법 피해 {magic * 100:.0f}%", f"앞라인 {front}명", f"이니시 {engage}명", f"CC 합계 {cc:.0f}"]
        warn = []
        if len(known) >= 3 and magic < 0.2:
            warn.append("AD 몰빵 (데이터상 마법 피해 20% 미만 팀 승률 45%)")
        if len(known) >= 4 and front == 0:
            warn.append("앞라인 없음 (승률 -1.5%p)")
        if len(known) >= 4 and engage == 0:
            warn.append("이니시 수단 없음 (승률 -2.5%p)")
        return ", ".join(notes) + (" | 주의: " + "; ".join(warn) if warn else "")

    # ---------- 상대 챔피언 라인 추정 (상대 라인은 밴픽 중에 안 보이니까)
    def assign_roles(self, enemy_ids, fixed):
        free_roles = [r for r in range(5) if fixed[r] is None]
        best, best_score = None, -1e18
        for roles in itertools.permutations(free_roles, len(enemy_ids)):
            sc = 0.0
            for c, r in zip(enemy_ids, roles):
                share = self.games.get(r, {}).get(c, 0) / max(1, self.role_total.get(r, 1))
                sc += math.log(share + 1e-5)
            if sc > best_score:
                best, best_score = roles, sc
        out = list(fixed)
        for c, r in zip(enemy_ids, best or []):
            out[r] = c
        return out

    # ---------- 추천
    def recommend(self, my_role, allies, enemies, bans=(), top=10):
        taken = {c for c in list(allies) + list(enemies) + list(bans) if c is not None}
        cands = {c: n for c, n in self.games.get(my_role, {}).items()
                 if c not in taken and n >= self.min_games}
        rows = []
        open_roles = [r for r in range(5) if enemies[r] is None]
        pools = {r: self.pool(r, taken, COUNTER_SHARE) for r in open_roles}
        for c in cands:
            us = list(allies)
            us[my_role] = c
            pairs = self.expected_pairs(us, enemies, taken | {c})
            team = self.expected_team(us, enemies, taken | {c})
            exp = pairs + team
            # 카운터 위험: 상대가 남은 라인에 '내 챔피언과의 상성'이 가장 나쁜 챔피언을 고를 때,
            # 그 라인의 평균적인 픽보다 얼마나 더 손해인지 (내 픽에만 해당하는 손해)
            worst_delta, worst_by = 0.0, None
            for r in open_roles:
                pool = {e: p for e, p in pools[r].items() if e != c}
                z = sum(pool.values()) or 1
                inter = {e: self.interaction(my_role, c, r, e) for e in pool}
                avg = sum(pool[e] * inter[e] for e in pool) / z
                for e, v in inter.items():
                    if v - avg < worst_delta:
                        worst_delta, worst_by = v - avg, (r, e)
            rows.append({"champ": c, "win": sigmoid(exp), "worst": sigmoid(exp + worst_delta), "worst_by": worst_by,
                         "games": self.games[my_role].get(c, 0), "parts": self.breakdown(us, enemies, my_role),
                         "team": team})
        if rows:
            avg_team = sum(r["team"] for r in rows) / len(rows)
            for r in rows:
                r["parts"].append(round(25 * (r["team"] - avg_team), 1))  # 다른 후보 평균 대비 조합 기여
        rows.sort(key=lambda x: -x["win"])
        return rows[:top]


# ---------------------------------------------------------------- 대화형 화면

def ask(prompt):
    try:
        return input(prompt).strip()
    except EOFError:
        sys.exit(0)


def parse_list(model, text, allow_empty_slots):
    out, errors = [], []
    for tok in text.replace(",", " ").split():
        if allow_empty_slots and tok in EMPTY:
            out.append(None)
            continue
        cid, err = model.find(tok)
        if err:
            errors.append(err)
        out.append(cid)
    return out, errors


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    path = os.path.join(HERE, "model.json")
    if not os.path.exists(path):
        print("model.json이 없어요. 먼저 모델학습.bat을 실행하세요.")
        return
    m = Model(path)
    print(f"=== 롤 밴픽 추천 === (학습 경기 {m.meta['matches']:,}판, 패치 {', '.join(m.meta['patches'])}, "
          f"그 라인 {m.min_games:,}판 이상인 챔피언만 추천)")
    print("챔피언 이름은 띄어쓰기 없이 입력하세요 (예: 리신, 미스포츈). 앞 글자만 써도 돼요.\n")
    while True:
        while True:
            role_txt = ask("내 라인? (탑/정글/미드/원딜/서폿): ")
            if norm(role_txt) in ROLE_ALIASES:
                my_role = ROLE_ALIASES[norm(role_txt)]
                break
            print("  탑, 정글, 미드, 원딜, 서폿 중에 입력하세요.")
        while True:
            txt = ask("우리 팀 (탑 정글 미드 원딜 서폿 순서, 아직 모르면 -): ")
            allies, errs = parse_list(m, txt, True)
            if errs:
                print("  " + "\n  ".join(errs))
                continue
            allies = (allies + [None] * 5)[:5]
            allies[my_role] = None
            break
        while True:
            txt = ask("상대 팀 (순서 상관없이 보이는 챔피언만, 없으면 엔터): ")
            enemy_ids, errs = parse_list(m, txt, False)
            if errs:
                print("  " + "\n  ".join(errs))
                continue
            enemy_ids = [e for e in enemy_ids if e is not None][:5]
            break
        while True:
            txt = ask("밴된 챔피언 (없으면 엔터): ")
            bans, errs = parse_list(m, txt, False)
            if errs:
                print("  " + "\n  ".join(errs))
                continue
            break
        enemies = m.assign_roles(enemy_ids, [None] * 5)
        if enemy_ids:
            print("  상대 라인 추정: " + ", ".join(f"{ROLE_KO[r]} {m.names[c]}" for r, c in enumerate(enemies) if c))
        rows = m.recommend(my_role, allies, enemies, bans)
        print(f"\n[{ROLE_KO[my_role]} 추천]  예상 승률 = 지금 조합 기준 / 카운터 당하면 = 상대가 남은 픽으로 이 챔피언 카운터를 골랐을 때")
        print(f"{'순위':>3}  {'챔피언':<10} {'예상 승률':>8} {'카운터 당하면':>12}   {'데이터':>7}   기여(챔피언/시너지/상성/조합 %p)")
        for i, r in enumerate(rows, 1):
            wb = f" ({m.names[r['worst_by'][1]]})" if r["worst_by"] else ""
            p = r["parts"]
            print(f"{i:>3}  {m.names[r['champ']]:<10} {r['win'] * 100:>7.1f}% {r['worst'] * 100:>8.1f}%{wb:<10} "
                  f"{r['games']:>6,}판   {p[0]:+.1f} / {p[1]:+.1f} / {p[2]:+.1f} / {p[3]:+.1f}")
        if rows:
            best = list(allies)
            best[my_role] = rows[0]["champ"]
            print(f"\n  1순위로 고르면 우리 팀: {m.team_notes(best)}")
        print("\n※ 조합만 본 확률이라 실력·숙련도 차이는 반영되지 않아요. 1~2%p 차이면 손에 익은 챔피언이 더 나아요.")
        if ask("\n엔터 = 새로 추천받기, q = 끝내기: ").lower() == "q":
            break
        print()


if __name__ == "__main__":
    main()
