"""Deterministic illustrative wafer inspection data; never research evidence.

Standard-library only. Forty attempted tests per die, explicit missing values,
latent mechanism labels separate from imperfect simulated inspection labels.
No real datasets are copied; sources inform taxonomy, not probabilities.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import random
import zipfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED = 7001305
GEOMETRY = dict(diameter_mm=300, die_width_mm=8, die_height_mm=6,
                scribe_mm=0.08, edge_exclusion_mm=3)
SOURCES = [
    dict(id="applied", title="Applied Materials · Defect Control", url="https://www.appliedmaterials.com/us/en/semiconductor/products/analyze/defect-control.html"),
    dict(id="imec", title="imec · EUV stochastic printing failures", url="https://www.imec-int.com/en/imec-magazine/imec-magazine-march-2018/imec-pushes-the-limits-of-euv-lithography-single-exposure-for-future-logic-and-memory-applications"),
    dict(id="onto", title="Onto Innovation · PrimaScan", url="https://ontoinnovation.com/products/primascan-system/"),
    dict(id="keysight", title="Keysight · Parametric Test Solutions", url="https://www.keysight.com/nl/en/products/semiconductors/parametric-test-solutions.html"),
    dict(id="cmp", title="Applied Materials · CMP dishing and erosion", url="https://ir.appliedmaterials.com/news-releases/news-release-details/applied-materials-launches-300mm-cmp-product-line-reflexion/"),
    dict(id="lam", title="Lam Research · Asymmetric wafer defects", url="https://newsroom.lamresearch.com/techniques-identify-correct-asymmetric-wafer-map-defects?blog=true"),
    dict(id="epi", title="Applied Materials · Epitaxy defects", url="https://ir.appliedmaterials.com/static-files/f2fbf48d-a761-4c4f-ab4c-73f312ab1ebc"),
    dict(id="gapfill", title="Applied Materials · HDP CVD gapfill", url="https://www.appliedmaterials.com/sg/en/product-library/centura-ultima-hdp-cvd.html"),
    dict(id="bond", title="Onto Innovation · Hybrid bonding process control", url="https://ontoinnovation.com/resources/enabling-in-line-process-control-for-hybrid-bonding-applications/"),
    dict(id="maps", title="MixedWM38 · Author repository (real + GAN augmented)", url="https://github.com/Junliangwangdhu/WaferMap"),
    dict(id="wm811k", title="WM-811K · Original lab download index", url="http://mirlab.org/dataSet/public/"),
    dict(id="secom", title="UCI · SECOM (CC BY 4.0)", url="https://archive.ics.uci.edu/dataset/179/secom"),
    dict(id="bright", title="Bright Data · Public-web Dataset Marketplace", url="https://brightdata.com/products/datasets"),
    dict(id="bright_ai", title="Bright Data · Custom datasets: public data only", url="https://brightdata.com/products/datasets/ai"),
]


def catalog():
    # Practical research inventory, not an exhaustive list for every material,
    # device architecture or package. Mechanism != observed electrical symptom.
    rows = [
        ("particle", "입자·이물", "표면/오염", "공정 전반", "광학 산란·SEM; 위치에 따라 전기 영향 다름", True, "applied onto"),
        ("residue", "세정·레지스트 잔사", "표면/오염", "세정/패터닝", "광학·SEM·표면 분석", True, "applied"),
        ("contamination", "표면 오염", "표면/오염", "세정/취급", "표면 분석·광학·전기 검사", True, "onto"),
        ("scratch", "스크래치", "기계/기판", "연마/취급", "광학·표면 검사", True, "applied onto"),
        ("pit", "표면 피트", "기계/기판", "기판/박막", "광학·표면 계측", False, "onto"),
        ("bump", "표면 돌기", "기계/기판", "기판/박막", "광학·표면 계측", False, "onto"),
        ("edge_chip", "웨이퍼 에지 칩핑", "기계/기판", "기판/취급", "에지 광학 검사", True, "onto"),
        ("crack", "웨이퍼 균열", "기계/기판", "기판/취급", "에지·표면·IR 검사", True, "onto bond"),
        ("stress", "박막·벌크 응력", "기계/기판", "박막/기판", "응력·형상 계측", False, "onto"),
        ("inclusion", "벌크 공극·내포물", "기계/기판", "기판", "벌크 민감 검사; 표면 검사만으로 부족", False, "onto"),
        ("bridge", "미세 브리지", "패터닝", "리소그래피", "SEM·연결성 검사; 단락 가능", True, "imec applied"),
        ("broken_line", "끊어진 라인", "패터닝", "리소그래피", "SEM·연결성 검사; 개방 가능", True, "imec"),
        ("missing_contact", "미싱 콘택트", "패터닝", "리소그래피", "SEM·저항/연결성 검사", True, "imec"),
        ("merged_contact", "병합 콘택트", "패터닝", "리소그래피", "SEM·누설/연결성 검사", True, "imec"),
        ("cd_shift", "CD 편차", "공정 편차", "리소그래피", "CD-SEM·전기 특성", True, "imec"),
        ("line_roughness", "라인 에지·폭 거칠기", "공정 편차", "리소그래피", "CD-SEM 국소 계측", False, "imec"),
        ("etch_bias", "비대칭 식각·증착", "공정 편차", "식각/증착", "구조 계측·웨이퍼 분포", True, "lam"),
        ("gapfill_void", "갭필 공극", "박막/배선", "절연막 증착", "단면/매립 구조 검사", True, "gapfill"),
        ("dishing", "CMP 디싱", "박막/배선", "CMP", "높이·두께 계측", True, "cmp"),
        ("erosion", "CMP 에로전", "박막/배선", "CMP", "높이·두께 계측", True, "cmp"),
        ("epi_nodule", "에피 노듈", "결정/에피", "에피택시", "표면/구조 검사", False, "epi"),
        ("epi_merge", "에피 조기 병합", "결정/에피", "에피택시", "구조 검사", False, "epi"),
        ("stacking_fault", "쌍정·적층 결함", "결정/에피", "에피택시", "결정/구조 분석", False, "epi"),
        ("oxide_failure", "게이트 산화막 무결성 이상", "전기 특성", "소자/웨이퍼 테스트", "누설·산화막 무결성 검사", True, "keysight"),
        ("junction_leakage", "접합 누설 이상", "전기 특성", "소자/웨이퍼 테스트", "I-V·누설 검사", True, "keysight"),
        ("contact_resistance", "라인·비아 저항 이상", "전기 특성", "배선/웨이퍼 테스트", "저항/전기 테스트 구조", True, "keysight"),
        ("vth_shift", "문턱 전압 편차", "전기 특성", "소자/웨이퍼 테스트", "트랜지스터 I-V 검사", True, "keysight"),
        ("tddb", "시간 의존 절연 파괴 TDDB", "신뢰성", "가속 스트레스", "시간·전압 스트레스 필요; 일반 sort로 확정 불가", False, "keysight"),
        ("bti", "바이어스 온도 불안정 BTI", "신뢰성", "가속 스트레스", "바이어스·온도·시간 스트레스 필요", False, "keysight"),
        ("hci", "핫 캐리어 열화 HCI", "신뢰성", "가속 스트레스", "스트레스 전후 소자 특성 비교 필요", False, "keysight"),
        ("electromigration", "일렉트로마이그레이션", "신뢰성", "가속 스트레스", "전류·온도·시간 스트레스 필요", False, "keysight"),
        ("bond_void", "접합 공극", "후공정/접합", "하이브리드 본딩", "IR·접합면 검사; 이번 미접합 웨이퍼 모델 밖", False, "bond"),
        ("bond_particle", "접합면 입자", "후공정/접합", "하이브리드 본딩", "표면 청정도·IR 검사; 모델 밖", False, "bond"),
        ("dicing_crack", "다이싱·픽업 균열", "후공정/접합", "절단/픽업", "다이·IR 검사; 이번 절단 전 모델 밖", False, "bond"),
    ]
    return [dict(id=i, name=n, family=f, stage=s, observable=o, modeled=m,
                 source_ids=src.split()) for i, n, f, s, o, m, src in rows]


PATTERNS = [dict(id=i, name=n, description=d) for i, n, d in [
    ("none", "뚜렷한 패턴 없음", "결함이 없다는 뜻은 아님"),
    ("random", "무작위", "산발적 결함; 원인을 유일하게 결정하지 않음"),
    ("local", "국소 군집", "제한된 영역에 집중"),
    ("center", "중앙", "중심 부근 집중"),
    ("donut", "도넛", "중간 반경의 띠"),
    ("edge_local", "에지 국소", "가장자리 일부에 집중"),
    ("edge_ring", "에지 링", "외곽의 띠"),
    ("scratch", "선형 스크래치", "길게 이어지는 선형 분포"),
    ("near_full", "거의 전면", "넓은 면적에 걸친 이상"),
    ("reticle_repeat", "필드 반복 예시", "작성한 노광 필드 크기의 반복; 실측 필드 아님"),
]]
LIMITATIONS = [
    "모든 수치·판정·위치는 seed로 생성한 합성 데이터이며 실제 팹 측정·기존 PVT 연구 결과가 아닙니다.",
    "결함 카탈로그는 실용적 조사 범위입니다. 모든 재료·소자·패키지의 모든 결함을 망라하지 않습니다.",
    "출처는 분류·검사 개념의 근거입니다. 확률·공정 편차·판정 한계·결함 효과는 작성한 예시이며 실측으로 보정하지 않았습니다.",
    "40회/다이 검사 시도; 누락 값은 null입니다. fail은 관측 한계 초과, inconclusive는 초과 없이 필수 검사 누락, pass는 전부 한계 내입니다.",
    "검출 결과는 민감도·오탐을 넣은 가상 센서 출력입니다. 생성 결함은 알려진 ground truth이며 인과 추론의 증거가 아닙니다.",
    "입자 등 검출 결함이 있어도 전기 판정이 pass일 수 있습니다. 공간 패턴은 고유한 원인과 일대일 대응하지 않습니다.",
    "3장 모두 같은 예시 lot입니다. 독립 팹·lot 검증이나 모델 학습 일반화, 수율 개선 효과를 입증하지 않습니다.",
    "신뢰성 스트레스·후공정 결함은 조사만 했으며 이번 웨이퍼 sort 생성에 포함하지 않습니다.",
    "8×6mm die/300mm wafer 형상은 작성한 배치입니다. 노치·에지 처리와 공정 구조를 제조용 설계로 사용할 수 없습니다.",
]


def positions():
    return [dict(row=r, col=c, x_mm=round(c*8.08, 2), y_mm=round(r*6.08, 2))
            for r in range(-24, 25) for c in range(-18, 19)
            if math.hypot(abs(c*8.08)+4, abs(r*6.08)+3) <= 147]


def test_definitions():
    tests = []
    defs = [("delay", "지연", "ps", 20, 155), ("leakage", "누설", "nA", 0, 150),
            ("contact", "배선/콘택트 저항", "ohm", 20, 110),
            ("vth", "문턱 전압", "V", 0.28, 0.64)]
    for v in (0.8, 1.0, 1.2):
        for temp in (-40, 25, 125):
            for channel, name, unit, lower, upper in defs:
                tests.append(dict(id=f"{channel}_{v:.1f}_{temp}", name=f"{name} {v:.1f}V / {temp}°C",
                                  channel=channel, vdd_v=v, temp_c=temp, unit=unit,
                                  lower=lower, upper=upper))
    for i, name, unit, low, high in [("cd", "CD", "nm", 20, 28),
                                    ("overlay_x", "Overlay X", "nm", -6, 6),
                                    ("overlay_y", "Overlay Y", "nm", -6, 6),
                                    ("film", "박막 두께", "nm", 90, 110)]:
        tests.append(dict(id=i, name=name, channel=i, vdd_v=None, temp_c=None,
                          unit=unit, lower=low, upper=high))
    return tests


def make_dataset(seed=SEED):
    rng = random.Random(seed)
    tests = test_definitions()
    measured, wafers = [], []
    mechanisms = catalog()
    modeled = [c["id"] for c in mechanisms if c["modeled"]]
    scenarios = [
        ("산발적 입자와 세정 국소 군집", ["random", "local"], "clean_A"),
        ("외곽 공정 편차와 중앙 누설 군집", ["edge_ring", "center", "random"], "etch_B"),
        ("취급 스크래치와 필드 반복 결함", ["scratch", "reticle_repeat", "edge_local"], "handling_C"),
    ]
    for w, (scenario, patterns, tool) in enumerate(scenarios):
        wafer_id = f"W{w+1:02}"
        wafer_shift = rng.gauss(0, 0.35)
        dies = []
        for p in positions():
            x, y = p["x_mm"], p["y_mm"]
            radius = math.hypot(x, y)
            local = math.exp(-((x+54)**2+(y-36)**2)/(2*19**2))
            edge = max(0, (radius-118)/29)
            center = math.exp(-(radius/33)**2)
            scratch = abs(y - 0.38*x + 17) < 5 and abs(x) < 115
            repeat = p["col"] % 3 == 1 and p["row"] % 4 == 2
            defects = []
            def add(i, probability):
                if rng.random() < probability and i not in defects:
                    defects.append(i)
            add("particle", 0.022 + (0.38*local if w == 0 else 0.01))
            add("residue", 0.28*local if w == 0 else 0.003)
            add("contamination", 0.14*local if w == 0 else 0.002)
            if w == 1:
                add("etch_bias", 0.72*edge)
                add("cd_shift", 0.46*edge)
                add("vth_shift", 0.30*edge)
                add("junction_leakage", 0.43*center)
                add("oxide_failure", 0.11*center)
                add("dishing", 0.12*edge)
                add("erosion", 0.07*edge)
            if w == 2:
                add("scratch", 0.82 if scratch else 0)
                add("broken_line", 0.33 if scratch else 0.003)
                add("bridge", 0.43 if repeat else 0.002)
                add("merged_contact", 0.24 if repeat else 0.001)
                add("missing_contact", 0.22 if repeat else 0.001)
                add("gapfill_void", 0.08 if repeat else 0.002)
                add("edge_chip", 0.36 if x > 113 and y > 20 else 0)
                add("crack", 0.13 if x > 113 and y > 20 else 0)
            add("contact_resistance", 0.008)
            severity = {i: rng.uniform(0.35, 1.0) for i in defects}
            # Shared wafer-scale field + die noise; neighboring dies correlate.
            field = wafer_shift + 0.5*math.sin(x/44) + 0.4*math.cos(y/38)
            delay = 76 + 2.6*field + rng.gauss(0, 2.5)
            leakage = math.exp(rng.gauss(math.log(3.1)+field*0.10, 0.28))
            contact = 55 + 1.8*field + rng.gauss(0, 2.2)
            vth = 0.43 + field*0.004 + rng.gauss(0, 0.008)
            cd = 24 + field*0.28 + rng.gauss(0, 0.35)
            ox, oy = field*0.6 + rng.gauss(0, 0.6), rng.gauss(0, 0.6)
            film = 100 + 0.7*field + rng.gauss(0, 0.8)
            for i, s in severity.items():
                if i in ("bridge", "merged_contact", "oxide_failure", "junction_leakage"):
                    leakage += 45*s
                if i in ("broken_line", "missing_contact", "contact_resistance", "gapfill_void"):
                    contact += 140*s; delay += 58*s
                if i in ("particle", "residue", "contamination") and rng.random() < 0.35:
                    leakage += 20*s; contact += 65*s
                if i in ("scratch", "crack", "edge_chip"):
                    contact += 100*s; delay += 75*s
                if i in ("etch_bias", "cd_shift"):
                    cd += 6.5*s; delay += 25*s; ox += 5*s
                if i == "vth_shift": vth += 0.18*s; delay += 40*s
                if i in ("dishing", "erosion"):
                    film -= 17*s; contact += 42*s
            detections = [i for i in defects if rng.random() < 0.82]
            if rng.random() < 0.008:
                candidates = [i for i in ("particle", "residue", "scratch") if i not in detections]
                if candidates: detections.append(rng.choice(candidates))
            die_id = f"{wafer_id}-R{p['row']+24:02}-C{p['col']+18:02}"
            failures, missing, metrics = [], 0, {}
            for test in tests:
                ch, v, t = test["channel"], test["vdd_v"], test["temp_c"]
                if ch == "delay": value = delay*(1/v)**0.85*(1+0.00135*(t-25)) + rng.gauss(0, 0.6)
                elif ch == "leakage": value = leakage*(v**2)*math.exp((t-25)/65) * math.exp(rng.gauss(0, 0.04))
                elif ch == "contact": value = contact*(1+0.0007*(t-25)) + rng.gauss(0, 0.5)
                elif ch == "vth": value = vth-0.00045*(t-25) + 0.01*(v-1) + rng.gauss(0, 0.001)
                else: value = {"cd": cd, "overlay_x": ox, "overlay_y": oy, "film": film}[ch]
                value = round(value, 6)
                if rng.random() < 0.0008:
                    value, status = None, "missing"; missing += 1
                else:
                    status = "pass" if test["lower"] <= value <= test["upper"] else "fail"
                    if status == "fail": failures.append(test["id"])
                if (v == 1.0 and t == 25) or ch in ("cd", "overlay_x", "overlay_y"):
                    key = {"delay": "delay_ps", "leakage": "leakage_na", "contact": "contact_ohm", "vth": "vth_v", "cd": "cd_nm", "overlay_x": "overlay_nm"}.get(ch)
                    if key: metrics[key] = value
                measured.append(dict(data_mode="synthetic", lot_id="SYN-LOT-001", wafer_id=wafer_id,
                                     die_id=die_id, row=p["row"], col=p["col"], x_mm=x, y_mm=y,
                                     tool_id=tool, test_id=test["id"], channel=ch, vdd_v=v, temp_c=t,
                                     value=value, unit=test["unit"], lower=test["lower"], upper=test["upper"], status=status))
            dies.append(dict(die_id=die_id, **p, bin="fail" if failures else "inconclusive" if missing else "pass",
                             fail_reasons=failures, metrics=metrics, defects=sorted(defects),
                             detections=sorted(set(detections)), tests_failed=len(failures), tests_missing=missing))
        bins = Counter(d["bin"] for d in dies)
        summary = dict(total=len(dies), **{b: bins[b] for b in ("pass", "fail", "inconclusive")},
                       defect_dies=sum(bool(d["defects"]) for d in dies),
                       detected_dies=sum(bool(d["detections"]) for d in dies),
                       measurement_count=len(dies)*len(tests),
                       successful_measurements=len(dies)*len(tests)-sum(d["tests_missing"] for d in dies),
                       defects=dict(sorted(Counter(i for d in dies for i in d["defects"]).items())))
        wafers.append(dict(wafer_id=wafer_id, scenario=scenario, patterns=patterns, lot_id="SYN-LOT-001", tool_id=tool, summary=summary, dies=dies))
    data = dict(schema_version=1, data_mode="synthetic", seed=seed,
                generator_version=1, geometry=GEOMETRY, limitations=LIMITATIONS,
                sources=SOURCES, catalog=mechanisms, tests=tests, patterns=PATTERNS,
                wafers=wafers, counts=dict(wafers=3, dies=sum(len(w["dies"]) for w in wafers),
                                          measurements=len(measured)),
                procurement=dict(brightdata="No wafer/die-defect offering found in public catalog search on 2026-10-04; not proof none exists. Aside content filter prevented dashboard inspection. No purchase or vendor inquiry.",
                                 recommendation="Use publicly available research datasets directly for reference; synthesized inspection data stays separate.",
                                 mixedwm38="Author describes real wafer maps with GAN-generated augmentation; not all samples are real measurements.",
                                 secom="1567 production entities, 591 feature columns according to UCI; no die coordinates supplied. Not a wafer-map replacement.",
                                 wm811k="Real wafer-bin-map research dataset; original lab endpoint unavailable during this lookup. License and download availability not verified."))
    assert set(i for w in wafers for d in w["dies"] for i in d["defects"]) <= set(modeled)
    return data, measured


def json_bytes(data):
    return (json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)+"\n").encode("utf-8")


def export(output: Path, web_json: Path, seed=SEED):
    data, measured = make_dataset(seed)
    output.mkdir(parents=True, exist_ok=True)
    web_json.parent.mkdir(parents=True, exist_ok=True)
    raw = json_bytes(data)
    (output/"synthetic-wafers.json").write_bytes(raw)
    web_json.write_bytes(raw)
    csv_io = io.StringIO(newline="")
    writer = csv.DictWriter(csv_io, fieldnames=list(measured[0]), lineterminator="\n")
    writer.writeheader(); writer.writerows(measured)
    csv_data = csv_io.getvalue().encode("utf-8")
    (output/"measurements.csv").write_bytes(csv_data)
    truth = dict(data_mode="synthetic", seed=seed, purpose="Oracle labels for validation only. Never input to blinded agent decisions.",
                 dies=[dict(wafer_id=w["wafer_id"], die_id=d["die_id"], defects=d["defects"]) for w in data["wafers"] for d in w["dies"]])
    (output/"ground-truth.json").write_bytes(json_bytes(truth))
    # Blinded machine-learning/agent input has no generator labels or scenario.
    observed = dict(data_mode="synthetic", geometry=GEOMETRY, tests=data["tests"],
                    wafers=[dict(wafer_id=w["wafer_id"], dies=[{k: v for k, v in d.items() if k != "defects"} for d in w["dies"]]) for w in data["wafers"]])
    (output/"observed-only.json").write_bytes(json_bytes(observed))
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(output.iterdir()) if p.name in ("synthetic-wafers.json", "measurements.csv", "ground-truth.json", "observed-only.json")}
    manifest = dict(schema_version=1, data_mode="synthetic", seed=seed, generator_version=1,
                    counts=data["counts"], test_count=40, missing_measurements=sum(d["tests_missing"] for w in data["wafers"] for d in w["dies"]),
                    catalog_count=len(data["catalog"]), modeled_catalog_count=sum(c["modeled"] for c in data["catalog"]),
                    limits="Authored illustrative limits, not fab specifications.", sha256=hashes)
    (output/"manifest.json").write_bytes(json_bytes(manifest))
    # Stable timestamps and sorted members make the archive byte reproducible.
    archive = web_json.parent/"synthetic-wafers.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name in sorted([*hashes, "manifest.json"]):
            info = zipfile.ZipInfo(name, date_time=(2026, 10, 4, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, (output/name).read_bytes())
    return dict(**manifest, wafer_summaries=[dict(wafer_id=w["wafer_id"], **w["summary"]) for w in data["wafers"]], archive_bytes=archive.stat().st_size)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT/"synthetic_wafers")
    parser.add_argument("--web-json", type=Path, default=ROOT/"web/data/synthetic-wafers.json")
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()
    print(json.dumps(export(args.output, args.web_json, args.seed), ensure_ascii=False, indent=2))
