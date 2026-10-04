# 검사 개선 v2 결과

동결된 모델을 새로운 60개 합성 lot(180장·234,900개 site), 2,880회 정책 실행으로 비교했다. 실제 SEM 이미지나 공장 성능을 측정한 결과가 아니다.

## 확인된 결과

개발 데이터로 먼저 선택한 후보는 `catboost_learned`다. 360 CU·광학 후보 안에서 평균 확인 DOI는 28.30개로, 강한 logistic learned 기준선 28.33개와 사실상 같았다. 짝지은 평균 차이는 −0.033개, lot bootstrap 95% 구간은 [−0.583, 0.550]다. 사전에 정한 20% 발견량 개선 목표는 달성하지 못했다. 5개 DOI까지의 비용도 2.63% 증가해 30% 비용 절감 목표를 달성하지 못했다.

후보 47,406개에서 분류 결과는 다음과 같다. threshold는 0.5이며, 광학 후보 밖 결함은 이 분류 표에 포함되지 않는다.

| 모델 | Precision | Recall | AP | Brier ↓ | ECE ↓ |
|---|---:|---:|---:|---:|---:|
| Logistic | 81.51% | 75.34% | 0.8723 | 0.07970 | 0.04577 |
| CatBoost | 81.56% | 76.89% | 0.8843 | 0.07461 | 0.01774 |
| CatBoost + isotonic | 79.53% | 79.10% | 0.8730 | 0.07507 | 0.01794 |

CatBoost의 확률 추정과 recall은 개선됐다. Isotonic 보정은 recall을 추가로 높였지만 precision과 AP는 낮아져 모든 지표가 함께 좋아지지는 않았다.

## 탐색적 결과와 다음 병목

이동 계획 정책은 29.07개를 발견해 같은 보정 모델의 learned 28.62개보다 0.45개 많았다. 95% 구간 [−0.133, 1.017]이 0을 포함한다. Logistic 기준선 대비 +2.59%는 사후 탐색 결과로, 이를 개발 단계의 선택을 대신하는 확정 승자로 발표하지 않는다. 기존 고정 감사 Falsify 대비 선택 후보의 +16.62%도 보조 비교다.

CatBoost의 3,915개 site 추론은 첫 호출 438.9 ms, warm 중앙값 6.68 ms(p95 9.23 ms)였다. Logistic warm 중앙값은 0.091 ms다. 분류 개선과 계산 속도 개선을 구분해야 한다. 이동 정책의 CPU 비용도 lot당 약 109 ms로 단순 learned보다 크다. 장비의 CU를 실제 초로 환산하지 않았다.

다음 실험은 실제 센서 관측의 검출 가능성과 이동 계획, 순위를 보존하는 확률 보정을 분리해 조사한다. 새로운 개발·검증 seed를 사용하며 v2 test 결과는 다시 후보 선정에 사용하지 않는다.

## 검증과 재현

145개 테스트가 통과했다. 독립 감사는 2,880개 실행의 106,667개 선택과 114,048개 센서 시도, 3,091개 감사 결정을 재계산했다. 예산 위반·정책 구성요소의 숨은 정답 노출·동결 확률 불일치는 모두 0건이며 기존 보호 파일 8개가 보존됐다.

- 동결 source: `7e64e95db102c71fbee57c2726616093b1f6f64f`
- 동결 receipt: `ac99238f00af278cd56ce79fdfed66ab0061ff2c4cbe3ba6492bb72f43b4d374`
- [구조화 결과](evidence/inspection-improvements-v2/results.json), [감사](evidence/inspection-improvements-v2/audit.json), [추론 시간](evidence/inspection-improvements-v2/inference-timing.json), [그래프](evidence/inspection-improvements-v2/summary.png)
- 전체 실행 경로와 파일 SHA는 [artifact manifest](evidence/inspection-improvements-v2/artifact-manifest.json)에 보존했다. [실행 안내](INSPECTION_V2_GUIDE.md)와 `scripts/audit_inspection_v2.py`로 재검증할 수 있다.

Jev는 genuine 검사 노트가 없어 이번 수치 실험에서 unavailable이다. 실제 API 성능과 반도체 검사 정확도를 주장하지 않는다.
