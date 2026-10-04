# 검사 성능 개선 v2 실행 안내

v1 연구·결과와 분리된 숫자 관측 기반 synthetic 연구다. 모델 강화, 독립 확률 보정, 감사 예산 배분, 두 단계 이동 계획을 비교한다. 실제 SEM 이미지 성능이나 장비 처리량을 의미하지 않는다.

## 설치와 실행

Python 3.12, numpy 2.3.3, CatBoost 1.2.10을 사용한다. 기존 앱 기본 의존성에 CatBoost를 강제하지 않고 선택 그룹으로 추가했다.

```powershell
uv sync --extra inspection
uv run --extra inspection python -m inspection_v2.cli develop --root 'C:\path\to\new-development'
uv run --extra inspection python -m inspection_v2.cli freeze --root 'C:\path\to\new-development'
uv run --extra inspection python -m inspection_v2.cli campaign --root 'C:\path\to\new-development'
uv run --extra inspection python -m inspection_v2.cli report --root 'C:\path\to\new-development'
```

`develop`는 새 디렉터리를 요구한다. 학습 12·보정 6·개발 20 lot을 생성하고 8개 variant × 2 mode × 3 budget을 비교한다. 후보 선정은 개발 데이터의 360 CU·candidate-only 평균 DOI, 평균 비용, ID 순서로 고정한다. 이 데이터의 비교 결과는 최종 성능 근거가 아니다.

`freeze`는 선택한 후보, 모델·소스·프로토콜·데이터 해시와 패키지 버전을 기록한다. 모든 소스를 검증한 뒤 `campaign`이 새로운 test 60 lot을 생성한다. 기존 실행은 재개하거나 덮어쓰지 않는다. `report`는 저장된 결과로만 문서를 작성한다.

## 구현과 역할

- [model.py](inspection_v2/model.py): train-only scaler, frozen CatBoost, 기존 설정의 logistic 기준선, 별도 lot의 isotonic 보정, 후보 범위 AP/Brier/ECE/risk-coverage.
- [policies.py](inspection_v2/policies.py): 기존 정책과 적응형 감사·경로 정책. 두 번째 행동에도 wafer 로딩·이동·dwell·재시도 비용을 예약한다. 보고된 성공 관측만 감사 utility를 업데이트한다.
- [harness.py](inspection_v2/harness.py): 매번 실제 남은 예산을 정책에 제공한다. 실패 비용, immutable 초기 확률, 유료 관측 경계는 v1과 동일하다. CatBoost에 가짜 온라인 업데이트를 수행하지 않는다.
- [jev.py](inspection_v2/jev.py): genuine note whitelist, 명시적 활성화, typed Choice/Noul, 캐시·보류·shadow 감사. 기본 HTTP 경로는 공식 endpoint만 허용하며 redirect를 따라가지 않는다.

Jev 모듈은 현재 숫자 하네스에 없는 실제 검사 노트를 요구한다. `integration_status([])`는 unavailable을 반환한다. API 키는 프로세스 환경변수 `TYPESAFE_API_KEY`에서 읽는다. credentials를 로그·payload·cache·모델 파일에 넣지 않는다. `JevClient(enabled=True)`를 명시한 경우에만 네트워크 요청이 발생하며 실제 서비스 지연·정확도는 별도 genuine-note 평가가 필요하다.

감사 샘플에 기록한 propensity는 **결정된 감사 분기 안에서 후보를 뽑을 조건부 확률**이다. 전체 정책의 off-policy 평가를 정당화하지 않는다.

## 검증과 결과 해석

모델 교체를 포함한 개선 효과는 원래 logistic learned 기준선과 비교하고, 같은 모델에서 검사 선택만 바뀐 효과는 동일 모델 learned와 비교한다. 기존 Falsify와의 차이도 보조 비교로 보고한다. 총 발견량·고확신 미검·후보 밖 발견·비용·실제 CPU 지연을 함께 기록한다. 불리한 ablation과 calibration 결과도 보존한다.

확정된 모델·정책이라도 현장 배포 성능을 의미하지 않는다. 광학 후보 밖 결함, sensor 한계, image/noise 차이, 실제 stage 비용은 별도 데이터로 검증해야 한다. 기존 v1 결과와 공개 UI에는 v2 숫자를 덮어쓰지 않는다.
