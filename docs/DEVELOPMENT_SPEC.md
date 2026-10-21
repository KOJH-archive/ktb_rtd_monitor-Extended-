# KTB RTD 의사결정 보조 도구 개발명세서

버전: 2026-10-21 실행시점 표준채권 DV01, 당일 외국인 수급·변동성 재실행 복원 및 현물자료 노후화 차단 반영. 기존 점검 결과와 이후 구현 상태를 기술하며 미래 계획을 완료 기능으로 포함하지 않는다.

## 1. 목적과 시스템 경계

CheckExpert RTD가 갱신하는 기존 Excel을 읽어 3년·10년 국채선물 호가를 수집하고, 장중 상대가격/호가잔량/수동 위험값을 사람이 해석할 수 있게 표시한다. 주문 송신·취소·체결조회·계좌 연결·원본 workbook 저장 기능은 없다. `research entry`는 재생기의 가상 상태이며 실제 주문이 아니다.

입력 신뢰 경계: RTD/Excel → 수집기, CSV → 신호 엔진, settings.json → 계산 설정. 각각 숫자/형식 검증은 수행하나 공급자 정합성, 거래소 시각, 입력 DV01의 경제적 정확성을 인증하지 않는다.

## 2. 기술스택 및 실행 환경

| 계층 | 기술 | 역할/현황 |
|---|---|---|
| OS/시세 | Windows, 데스크톱 Excel, CheckExpert RTD | 사용자가 로그인·Excel·애드인을 먼저 실행. 공급자 API 직접 연동 없음 |
| 런타임 | CPython 3.12.13, 32bit `.venv32` | 이번 자동시험 실행 환경. 다른 Python/비트수 조합은 이번에 검증하지 않음 |
| Excel 접근 | xlwings 0.33.22, pywin32 312 | 설치 메타데이터 확인. 수집 시 lazy import, 테스트/UI는 COM 초기화 불필요 |
| 의존성 선언 | `xlwings>=0.32,<0.34`, Windows `pywin32>=306` | 정확한 lockfile은 없음. 재현 배포 시 테스트한 버전으로 별도 환경 고정 필요 |
| 계산 | statistics, math, collections.deque, dataclasses | NumPy/Pandas/ML 프레임워크 없음 |
| 저장 | csv, SQLite(sqlite3), WAL, synchronous=NORMAL | 로컬 파일, CSV/UI와 SQLite/보관 이중 출력. 분산 DB 없음 |
| UI | Tkinter, 500ms after callback | 로컬 데스크톱 화면, 웹서버·다중 사용자 기능 없음 |
| 품질 검증 | unittest, unittest.mock, hashlib | 실제 데이터 재생, 품질 감사, 입력/코드 SHA-256 |
| 기동 | Windows BAT, Python supervisor, argparse CLI | `KTB_모니터_시작.bat` 한 번으로 수집기와 UI를 함께 시작·종료. 요약·재생은 필요 시 Python CLI로 실행 |

## 3. 모듈과 데이터 흐름

```text
CheckExpert → Excel Sheet1 + 추가RTD (사용자가 연결 유지)
                   ↓ 기본 호가 C2:O11 한 번 + 연결된 추가 지표 범위 읽기
ktb_rtd_monitor.py: 검증 → 호가 정규화 → 지표 → 변경/heartbeat 선별
                   ├→ data/snapshots_enhanced_YYYYMMDD.csv → dashboard.py 당일 증분 읽기
                   │                              ↓
                   │                         signal_engine.py
                   │                     ┌────────┴─────────┐
                   │              이벤트 RV / 완료분 RV   합의·비용·위험
                   │                     └────────┬─────────┘
                   │                              ↓ 관찰 화면 (주문 없음)
                   └→ 같은 날짜의 SQLite (보관)

동일 CSV → paper_backtest.py → 동일 signal_engine → 다음 관측 가상 체결/미청산 평가
동일 CSV → audit_snapshots.py → 품질·창 시간폭·재현 해시 보고서
settings.json → 공통 설정 검증 → UI/신호/재생
```

| 파일 | 책임/주요 인터페이스 |
|---|---|
| `ktb_rtd_monitor.py` | `ExcelRtdReader.snapshot`, `_metrics`, `SnapshotThrottle`, `SnapshotLogger`; 읽기/수집/저장 |
| `analyze_snapshots.py` | `RollingRVAnalyzer`, `MinuteRVAnalyzer`, `ConfirmedConsensus`; 통계·상태 전이. CLI 요약은 별도의 단순 이벤트 통계이며 전체 거래판단기가 아님 |
| `signal_engine.py` | `validated_row`, `SignalEngine.update → SignalResult`; UI/재생 공통 검증·초기화·워밍업·연구 신호 |
| `decision_support.py` | 호가잔량 압력, 수동 적정가 비교, 가상 패키지 위험, 비용필터, 가상 bid/ask 손익 |
| `app_settings.py` | frozen dataclasses, 설정 로드/검증, DV01 패키지 및 beta |
| `futures_dv01.py` | KRX 표준채권 현금흐름 가격, 중앙차분·해석적 DV01 검산, RTD 수익률 쌍 검증 |
| `dashboard.py` | bounded CSV tail, 지표 표시, 데이터 지연·오류 시 사용 금지 표시 |
| `monitor_launcher.py` | 단일 실행 잠금, 숨김 수집기 시작, 대시보드 수명주기 감독, 함께 종료, 2MB 실행 로그 회전 |
| `paper_backtest.py` | 가상 pending/position 상태, 다음 관측 체결, 비용 분리·미청산 표시 |
| `audit_snapshots.py` | 입력 정합성·공백·이벤트 시간폭 집계, 해시 기록 |
| `test_metrics.py`, `test_safety.py` | 기존 수학 회귀시험 + 안전성/체결타이밍/저장 검증 |
| `market_extensions.py` | 체결가차 계산, 임계값 상태기계, Windows 점멸/저음, 외국인 누적량 차분 추정 |
| `extra_settings.py`, `extra_settings.json` | 추가 RTD 셀 연결 및 사용자 가격 알림 규칙 |

## 4. 입력 데이터 계약

Excel 파일명 기본값은 `국채선물 호가창.xlsx`, 시트는 `Sheet1`이다. 열 배치는 고정되어 있으며 변경되면 수집 코드도 수정해야 한다. 열린 workbook을 파일명으로 찾으므로 동일 파일명의 여러 workbook이 열려 있지 않도록 운영해야 한다.

| 데이터 | 3Y (`KBFA020`) | 10Y (`KXFA020`) |
|---|---|---|
| 매도 가격, 잔량 | D2:D6, C2:C6 | L2:L6, K2:K6 |
| 매수 가격, 잔량 | D7:D11, E7:E11 | L7:L11, M7:M11 |
| 마지막 체결가 | G2 | O2 |

한 번의 `C2:O11` 값 읽기로 두 종목을 가져온다. COM 왕복을 줄이지만 RTD 계산 자체의 원자적 동시성을 보장하지 않는다. `timestamp_utc`는 **읽기 직후 PC UTC 시각**이며 거래소 체결시각이 아니다. 체결가 셀 값 변화가 개별 체결 이벤트를 완전하게 나타내지도 않는다.

수집 검증: 기본 호가 가격/잔량은 유한한 양수, 각 측 5개, 가격 순서 정상, best ask > best bid. Excel의 ask는 5호가→1호가이므로 반전한다. 기본 호가의 빈 값·문자열 오류·bool·NaN/Infinity·0/음수·역전·잠김은 해당 스냅샷 저장을 보류한다. 반면 마지막 체결가나 추가 RTD 현물·선물수익률이 0·공란·비정상이면 해당 선택 지표만 공란(`N/A`)으로 저장하고 정상 호가 수집은 계속한다. 외국인 누적 매수·매도 수량의 0은 유효하다. 잔량 0이나 locked book이 정상적으로 존재할 수도 있으나 현재 연구기는 보수적으로 분석 불가로 취급한다. 실제 시장 단계 구분은 없다.

기본 호가 로그 열은 총 61개: 시각 1개 + 각 종목 30개. 종목별 체결가 1개, best_bid/ask·mid·spread·bid/ask_depth·OBI·WOBI·microprice 9개, 5단계 가격·잔량 20개다. 현재의 추가 RTD 활성 로그에는 선택 지표와 파생 금리차 열이 더 붙으며, 값이 공란이어도 열 구성은 유지한다. 가격 단위는 선물 호가 포인트, 잔량은 공급자의 계약 수 단위다. DV01은 원/bp/계약이며 수익률 퍼센트가 아니다.

`추가RTD`의 현재 연결은 B1/B2 현물 3Y/10Y 체결수익률(%), B3/B4 3Y 외국인 누적 매수/매도, B6 3Y 선물수익률(%), B7/B8 10Y 외국인 누적 매수/매도, B10 10Y 선물수익률(%)이다. B5/B9 선물가격도 존재하지만 본 수집기의 기존 체결가 셀 G2/O2와 같은 15001 필드이므로 중복 저장하지 않는다. 추가 연결 활성화 시 새 로그 `snapshots_enhanced`를 사용하며 기존 61열 로그를 변경하지 않는다.

공통 엔진은 timezone 포함 시각, 두 종목의 유한 양수 mid/best bid/ask/spread, 양의 스프레드, bid/ask와 mid/spread 일치성을 검증한다. 나머지 호가 필드 전체를 재계산하지는 않는다. UI는 추가 표시 지표의 유한성도 검사한다.

## 5. 계산식

### 5.1 호가 지표

```text
mid = (best_bid + best_ask) / 2
spread = best_ask − best_bid
bid_depth = Σ bid_qty[1..5], ask_depth = Σ ask_qty[1..5]
OBI = (bid_depth − ask_depth) / (bid_depth + ask_depth)
weights = [1.0, 0.7, 0.5, 0.3, 0.2]  # 가까운 호가부터
WOBI = (Σ weights×bid_qty − Σ weights×ask_qty) / (Σ weights×bid_qty + Σ weights×ask_qty)
microprice = (best_ask×bid_qty1 + best_bid×ask_qty1) / (bid_qty1 + ask_qty1)
```

microprice는 잔량가중 중심값이지 체결가격 예측모델이 아니다. 현재 화면의 pressure 점수는 `0.3×OBI + 0.5×WOBI + 0.2×clip((microprice−mid)/(spread/2),−1,1)`을 가중치 합으로 나눈 후 EMA를 적용한다. 해당 종목의 입력 지표가 바뀐 경우에만 EMA를 갱신한다. 동일 heartbeat 수에 따라 점수가 변하지 않지만 실제 시장 이벤트 속도에 따른 시간 길이는 고정되지 않는다. 상대 점수는 10Y−3Y이며 ±0.25는 미검증 연구 임계값이다.

### 5.2 RV와 시간 기준

```text
beta = DV01_10Y / DV01_3Y  # 수동 DV01 또는 실행시점 자동 표준채권 DV01
       또는 rv.beta (수익률 미연결 시 기본 2.88, 연구용)
RV_t = mid10_t − beta×mid3_t
Z_fast = (현재 RV − 최근 이벤트 RV 평균) / 표본 표준편차(ddof=1)
Z_core = (현재 RV − 최근 N개 완료 분 RV 평균) / 표본 표준편차(ddof=1)
```

빠른 창은 최초 관측 이후 두 mid 중 하나가 달라질 때만 추가하며, deque는 최대 120개다. 현재 관측을 빠른 창의 평균/표준편차에 포함한다. 완료 분 기준은 해당 분에 마지막으로 관측한 RV를 다음 분 진입 시 확정한다. 현재 진행 중인 분은 참조 평균에 포함하지 않는다. 누락된 분은 이전 값으로 채우지 않는다. 최초 수집 분은 부분 분일 수 있으므로 60개 분 종가가 반드시 60분 연속 관측시간을 뜻하지는 않는다.

실시간 처리 중 `max_gap_seconds` 초과 공백·KST 달력 날짜 변경 시 두 창과 합의 상태를 초기화한다. 다만 대시보드 재실행 때 당일 CSV를 복원하는 과정의 공백은 기존 관측창을 보존하고, 공백을 가로지르는 이벤트 변화와 1분 변화는 만들지 않으며 합의 상태만 초기화한다. 역순/동일 시각·불량 행도 실시간에서는 오류로 초기화한다. 날짜 변경은 거래소 세션 달력 구현이 아니다. 특히 KRX 안내에는 야간 세션이 있으므로 자정 초기화를 야간 거래일 처리로 오해해서는 안 된다. [KRX 3년 상품명세](https://global.krx.co.kr/contents/GLB/02/0201/0201040501/GLB0201040501.jsp)

두 창의 표본 수를 충족하고 표준편차가 각각 1e-12보다 커야 ready다. 연구 상태는 `−1, 0, +1`이며 실제 보유 포지션과 무관하다.

1. state=+1이고 두 Z가 exit_z보다 크면 유지. −1은 두 Z가 −exit_z보다 작으면 유지.
2. 두 Z가 entry_z 이상이면 +1 후보, 두 Z가 −entry_z 이하면 −1 후보, 아니면 0 후보.
3. 후보가 바뀌면 확인 시간을 새로 시작. 같은 후보가 관측시각 기준 3초 지속되면 상태 변경.
4. heartbeat도 확인시간 판단에 사용한다. 관측 사이에 조건이 계속 성립했다는 것은 샘플링 가정이지 거래소 틱 단위 증명이 아니다.
5. 품질·워밍업·분산 조건 미충족 시 state=0. `entry_allowed`는 비용 및 설정 가상 위험한도 검사 결과이며 주문 승인이 아니다.

### 5.3 DV01 및 수량

```text
P_T(y) = Σ[i=1..2T] coupon_i/(1+y/2)^i + 100/(1+y/2)^(2T)
coupon_i = 2.5, 마지막 현금흐름에는 원금 100 추가
DV01_KRW = {P_T(y−1bp) − P_T(y+1bp)} / 2 × 1,000,000
exact_3Y = N10 × DV01_10Y / DV01_3Y
rounded_3Y = max(1, round(exact_3Y))
residual = abs(rounded_3Y×DV01_3Y − N10×DV01_10Y)
gross = rounded_3Y×DV01_3Y + N10×DV01_10Y
```

자동 계산은 KRX 표준 조건인 액면 1억원, 연 5% 쿠폰, 반기 지급, 잔존 3년·10년을 사용한다. 중앙차분 결과는 동일 현금흐름의 해석적 가격미분과 자동시험에서 교차검증한다. 대시보드 실행 시 수동 DV01이 모두 비어 있고 최신성 한도 안의 양수 선물수익률 두 개가 있으면 계산하며, 기준 수익률·시각과 함께 표시하고 세션 동안 고정한다. 수동 DV01 쌍은 자동값보다 우선한다. 이는 표준채권의 이론 민감도이지 실제 보유채권, 거래 바스켓, 계좌 또는 포트폴리오 DV01 인증값이 아니다.

자동 실행시점 DV01은 대시보드 세션에만 주입한다. 저장 CSV를 별도 실행하는 `paper_backtest.py`와 `analyze_snapshots.py`는 재현성을 위해 `settings.json`의 수동 DV01 또는 `rv.beta`를 계속 사용하며, 당시 자동값을 별도 저장하지 않은 과거 자료에 현재 DV01을 소급 적용하지 않는다.

Python의 반올림(정확한 .5는 짝수 방향)을 사용한다. 최적 정수 패키지 탐색은 아니며 3Y 최소 1계약 가정도 연구 편의다. 순 DV01 부호, 실제 보유 방향, 포트폴리오 노출은 표시하지 않는다. 가상 정수 패키지 한도와 재생의 소수 beta 패키지는 서로 다른 수량 모델이다.

### 5.4 수동 적정가 비교 및 비용필터

```text
manual_difference = market_mid − manual_fair
comparison_cost = futures_spread/2 + manual_extra_cost
display_net_distance = max(0, abs(manual_difference) − comparison_cost)
mean_distance = abs(current_RV − completed_minute_mean)
estimated_roundtrip = spread10 + beta×spread3 + execution.extra_roundtrip_cost_points
required_distance = estimated_roundtrip × min_edge_to_roundtrip_cost
```

수동 비교에는 현물 레그의 비용·조달·잔존만기 모델이 없다. fair는 시작 시 읽은 정적 값이며 자동 갱신·시점 검증이 없다. `mean_distance`는 기대수익이나 정상화 확률이 아니다. 진입 관측시점의 호가로 왕복비용을 근사할 뿐 실제 청산시점 비용은 모른다. 미입력 비용은 0이며 수수료/시장충격/펀딩이 실제로 무료라는 뜻이 아니다.

## 6. 페이퍼 재생 상태 및 손익

신호 시각 t → pending 의도 저장 → 이후 다음 유효 시각 t+1의 best bid/ask로 가상 체결한다. 새 행의 신호가 기존 pending 의도를 취소하지 않도록, 실행 판단과 새 신호 생성의 의미를 분리한다. 단, 불량 데이터·시각 공백·날짜 전환에서는 pending을 취소한다. 체결이 가능한지 잔량을 검사하지 않으므로 이 방식도 낙관적인 관측 호가 가정이다.

RV high(+1)는 가상 short 10Y 1단위 / long 3Y beta단위, RV low(−1)는 반대다. 반전 시 다음 행에서 청산과 반대 진입이 동시에 발생할 수 있으며 양쪽 비용을 각각 계산한다. 기본 N10=1 단위 연구이며 risk.package_10y_contracts가 실제 재생 수량으로 곱해지지 않는다.

```text
high 가상 손익 = (entry_bid10 − exit_ask10) + beta×(exit_bid3 − entry_ask3)
low 가상 손익  = (exit_bid10 − entry_ask10) + beta×(entry_bid3 − exit_ask3)
gross_mid = −state×(exit_RV − entry_RV)
crossing_cost = gross_mid − bidask_PnL
net = bidask_PnL − configured_extra_roundtrip_cost
```

단위는 synthetic points다. 원화 실현손익·수익률·샤프·증거금 수익률은 계산하지 않는다. 소수 계약 가정과 실제 정수 수량의 차이가 있어 단순 금액 환산만으로 실제 포지션 성과가 되지 않는다.

데이터 끝·공백·오류·신호 준비 해제 시 남은 가상 포지션은 `unclosed_mark_only`로 기록한다. 이때 `exit_utc`와 `net_pnl_points`는 비우고 `mark_utc`, `mark_net_pnl_points`에 마지막 관측 평가만 남긴다. 이는 실제 포지션 청산 명령이나 복구 가능한 계좌 장부가 아니다. 이후 정상 구간 연구는 새 가상 에피소드로 시작한다. 집계 손익은 `status=closed`만 포함한다.

출력은 `open('x')`로 새 파일만 생성하며 기존 보고서를 덮어쓰지 않는다. BAT는 임의 접미사 파일을 생성한다. 구버전 `paper_trades.csv`는 보존하며 현행 성과와 섞지 않는다.

## 6A. 가격 알림과 외국인 수급 추정

```text
선물 체결가차(pt) = 10Y trade price − 3Y trade price
현물 금리차(bp) = (10Y cash yield[%] − 3Y cash yield[%]) × 100
```

알림은 `above`, `below`, `between` 조건을 지원한다. 최초 조건 충족 뒤 confirmation_seconds 동안 유지되어야 발생하며, 발생 후 비활성화된다. 기준값에서 hysteresis만큼 이탈해야 재무장하고 cooldown_seconds도 경과해야 한다. 파일 최초 warm-up 행, 지연/미래 수집시각, 연결된 원천시각이 오래되었거나 두 레그 시각 차이가 큰 경우에는 알리지 않는다. 원천시각이 연결되지 않은 현재 상태에서는 수집시각만 검사하므로 RTD 자체 동결은 탐지하지 못한다.

알림 출력은 Windows `FlashWindowEx`의 작업표시줄 점멸 5회와 620Hz·90ms·기본 peak 6%의 생성 WAV다. 화면을 가리는 자동 팝업을 만들지 않는다. 소리는 별도 daemon thread에서 재생한다. 알림은 관찰 기능이며 주문과 연결되지 않는다.

외국인 추정치는 누적 매수·매도 증가량을 직전 관측 이후 발생한 수량으로 보고 현재 체결가/선물수익률을 대용가격으로 사용한다. 시작 시 기존 누적량은 버리고 baseline만 설정한다. 각 측의 가격·수익률 추정 평균은 증가수량 가중평균이며 가격은 기본 0.02pt 구간으로 집계한다. 누적량 감소나 세션 변경은 당일 추정 이력을 초기화한다. 시각 역행·10초 초과 공백·입력 불량은 이전 관측 이력과 아직 시간창 안에 있는 수급을 보존하지만 연속성 기준을 다시 시작하며, 공백 중 누적 증가분은 가격에 배정하지 않는다. 참가자 체결원장·거래대금·개별 체결시각이 없으므로 실제 외국인 평균체결가, 보유원가, 미결제약정 원가로 표현하지 않는다.

## 7. 설정 명세

누락된 파일은 오류, 누락된 섹션/필드는 기본값 적용. 알 수 없는 키·비객체 섹션·bool/문자열 숫자·NaN/Infinity는 거부한다. 설정은 프로세스 시작 시 로드하므로 변경 후 UI/재생을 다시 시작해야 한다. 수집 간격은 settings가 아니라 수집기 CLI로 정한다.

| 경로 | 기본값 | 단위/제약 |
|---|---|---|
| rv.beta | 2.88 | 양수; DV01 쌍 입력 시 대체됨 |
| rv.fast_event_window | 120 | 정수 ≥2, 변화 관측 개수 |
| rv.core_minutes | 60 | 정수 ≥2, 완료 분 개수 |
| rv.entry_z / exit_z | 2.0 / 1.5 | 양수, exit < entry |
| rv.confirmation_seconds | 3.0 | 양수 초 |
| data.max_gap_seconds | 10.0 | 양수 초; 초과 시 재준비 |
| data.stale_seconds | 10.0 | 양수 초; UI 현재 시각과 비교 |
| risk.dv01_3y_krw_per_bp / dv01_10y_krw_per_bp | null / null | 둘 다 null이면 신선한 RTD 선물수익률로 실행시점 표준채권 DV01 자동 계산; 둘 다 양수면 수동값 우선, 원/bp/계약 |
| risk.package_10y_contracts | 1 | 정수 ≥1; 위험 표시 패키지 크기 |
| risk.max_gross_dv01_krw_per_bp | null | 선택적 양수 원/bp |
| risk.max_residual_dv01_krw_per_bp | null | 선택적 양수 원/bp |
| risk.cash_history_max_age_days | 4 | 정수 ≥1; 마지막 현물금리와 계산 기준일의 달력 일수 초과 시 수량 차단 |
| flow.ema_alpha | 0.2 | 0 < alpha ≤ 1 |
| flow.obi_weight / weighted_obi_weight / microprice_weight | 0.3 / 0.5 / 0.2 | 각 양수, 합으로 정규화 |
| flow.relative_threshold | 0.25 | 양수 |
| basis.fair_3y_price / fair_10y_price | null / null | 선택적 양수, 선물 호가 포인트 |
| basis.extra_cost_3y_points / extra_cost_10y_points | 0 / 0 | 유한 0 이상 |
| basis.min_net_edge_points | 0.01 | 양수 |
| execution.min_edge_to_roundtrip_cost | 1.25 | 양수 배수 |
| execution.extra_roundtrip_cost_points | 0 | 유한 0 이상, 합성 패키지 왕복 포인트 |

## 8. 저장·성능·오류 처리

수집 기본 1000ms, 허용 100~2000ms. 정상 관측과 단순 시세 무효는 매 루프 시작시각 기준으로 다음 읽기까지 최소 간격을 맞춘다(Excel 읽기가 설정 간격보다 길면 추가 대기 없음). `ExcelConnectionUnavailable`(workbook/시트 미발견, Excel/COM 읽기 예외)가 연속되면 `PollSchedule`이 오류 감지 후 3초→4초→5초(상한 5초)를 실제로 기다렸다가 재시도한다. 첫 정상 스냅샷부터 설정된 정상 간격으로 돌아간다. 0·공란·비정상 호가처럼 읽기는 됐지만 데이터가 유효하지 않은 `FeedUnavailable`은 연결 오류와 구분하며 정상 간격으로 재확인한다. 오류 메시지는 같은 문구를 60초 이내 반복 출력하지 않고 KST 오류 시각과 다음 재시도 간격을 `launcher.log`에 남긴다. 이 조절은 Python의 Excel 읽기 빈도만 바꾸며 공급자 RTD 구독/갱신 주기를 제어하지 않는다. 1초 사이 잠깐 나타난 가격은 관측되지 않을 수 있다.

변동 행 또는 5초 heartbeat 저장. CSV flush/SQLite commit 기본 1초. 강제 종료/정전 시 미반영분 유실 가능하며 CSV와 SQLite 간 원자적 트랜잭션은 없다. SQLite 열은 현재 TEXT다. 운영용 스키마 버전·인덱스·중복키·보존주기·파일 회전은 미구현이다.

CSV/SQLite 기존 열 순서가 예상과 다르거나 CSV 마지막 줄이 불완전하면 이어쓰기를 거부한다. 자동 변환·삭제하지 않는다. 오류 시 새 출력 폴더를 사용하거나 백업 후 별도 복구해야 한다. 여러 수집기를 같은 폴더에 동시에 실행하지 않는다(프로세스 잠금 미구현).

UI는 KST 오늘 날짜의 CSV만 선택한다. 시작 시 마지막 1MiB에서 최신의 완전한 행을 역순 탐색해 신선한 선물수익률 쌍이 있으면 자동 DV01과 고정 beta를 먼저 구성한다. 그 뒤 오늘 파일을 500행 단위로 읽어 외국인 수급과 동일 beta의 변동성/RV 분석창을 복원한다. 최신 행이 오래됐거나 수익률이 0·공란이면 자동 계산하지 않고, 새 정상 행이 들어올 때 다시 시도한다. 복원 중 동일 날짜의 10초 초과 공백은 관측 이력을 삭제하지 않고 연속성만 끊는다. 따라서 공백을 가로지르는 RV 변화·외국인 증가량은 만들지 않으며, 1분 변동성은 실제 인접 분 변화만 사용한다. 복원 중 과거 알림은 발생시키지 않고 과거 합의 신호 상태도 완료 시 폐기해 새 관측부터 다시 확인한다. 금리차 위치·호가압력·수동 적정가 등 다른 탭의 상태는 과거 행으로 복원하지 않는다. 복원 중 추가된 행도 따라잡는 동안 알림을 재생하지 않는다. 이후 일반 tail은 한 번에 최대 1MiB씩 추가분을 읽고 마지막 불완전 줄은 다음 읽기까지 보관한다. 파일 축소·파일 식별자 변경 시 재준비한다. 같은 inode에서 크기를 유지하며 내용을 덮어쓰는 경우까지 탐지하지 않는다.

UI는 데이터 지연/미래 시각이면 현재 신호 사용 금지를 표시한다. 읽기·형식 오류는 기존 상태를 초기화하고 오류를 표시한다. 과거 지표 숫자는 참고 화면에 남을 수 있으나 활성 신호가 아니다. 수집시각이 신선해도 원천 RTD가 동결된 상태일 수 있으며 이 문제는 원천 heartbeat/시각 없이는 해결되지 않는다.

수집기가 잠깐 거부한 입력은 CSV에 기록되지 않는다. 따라서 10초 이하의 원천 오류 이력이 신호 엔진에서 보이지 않을 수 있다. 과거 CSV 안에 직접 존재하는 불량 행을 초기화하는 것과 공급자 오류 이력의 완전한 동기화는 다르다. 공급자 품질 이벤트/세션 식별자를 로그에 추가하는 것은 후속 작업이다.

저부하 설계는 bounded deque, 변경 기반 EMA, 이벤트 통계 캐시, COM 일괄 읽기, 증분 UI, 파일 핸들 재사용으로 구현했다. 실제 Excel CPU/메모리·오랜 장중 가동·복구 지연 수치는 이번에 측정하지 않았다.

## 9. 실행과 재현

프로젝트 폴더에서 실행한다. `output`은 존재하지 않는 새 파일명을 사용한다.

```powershell
.\.venv32\Scripts\python.exe -m unittest -v
.\.venv32\Scripts\python.exe -m compileall -q app_settings.py analyze_snapshots.py signal_engine.py decision_support.py dashboard.py ktb_rtd_monitor.py paper_backtest.py audit_snapshots.py
.\.venv32\Scripts\python.exe audit_snapshots.py data/snapshots_with_trades.csv --settings settings.json --output data/audit_new.json
.\.venv32\Scripts\python.exe paper_backtest.py data/snapshots_with_trades.csv --settings settings.json --output data/replay_new.csv
```

실시간 실행은 Excel 연결을 사용자가 먼저 확인하고 `KTB_모니터_시작.bat`을 한 번 실행한다. BAT는 `pythonw.exe`로 `monitor_launcher.py`를 시작하므로 별도 콘솔을 유지하지 않는다. 실행기는 `data/monitor_launcher.lock`에 비차단 Windows 파일 잠금을 잡아 같은 폴더의 중복 통합 실행을 막는다. 수집기는 콘솔 없이 자식 프로세스로 실행되고 stdout/stderr는 `data/launcher.log`에 기록된다. 로그가 시작 시점에 2MB 이상이면 기존 `launcher.log.old`를 교체하고 현재 로그를 그 이름으로 옮겨 무제한 누적을 막는다. 1초 뒤 수집기가 살아 있으면 대시보드를 시작한다. 대시보드가 종료되면 수집기를 정상 종료 요청하고 5초 안에 끝나지 않을 때만 강제 종료한다. 잠금은 프로세스가 종료되면 운영체제가 해제하므로 잠금 파일이 남아 있어도 실행 상태를 뜻하지 않는다. 개발자가 수집기를 CLI로 별도 실행하는 경우까지 중복 방지하는 잠금은 아니다.

테스트는 모의 Excel 객체를 사용하며 실시간 데이터 연결 성공을 뜻하지 않는다. 상세 검증 결과와 미완료 항목은 [FINAL_REVIEW.md](FINAL_REVIEW.md), 업무 적용 경계는 [TRADING_SCOPE.md](TRADING_SCOPE.md)를 따른다.
