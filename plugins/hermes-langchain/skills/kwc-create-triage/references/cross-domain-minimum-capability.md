# 최소 cross-domain capability 정책

미구현 producer 도메인이 consumer 도메인의 작업을 막는다고 해서 producer 전체를 선행 구현하지 않는다. PM은 consumer의 관찰 가능한 acceptance가 실제로 필요로 하는 사실만 producer-owned public contract로 고정한다.

## 판단 순서

1. consumer가 필요한 capability와 acceptance를 먼저 적는다.
2. producer의 공개 interface/query/event, 실제 adapter, 오류·consistency 의미를 확인한다. Entity, Repository, controller, schema는 public contract 증거가 아니다.
3. 실제 seam이 있으면 `published`로 기록한다. `producer_evidence`에는 committed public surface와 실제 producer 경로 locator를 넣는다.
4. seam이 없거나 부분 구현이면 `absent-or-partial`로 기록하고 Decision을 만든다.
5. Decision이 승인되면 실제 상태는 계속 `absent-or-partial`이다. PM은 최소 contract와 producer·consumer·integration follow-up을 고정한 뒤 graph를 만들 수 있다.
6. producer/consumer는 frozen minimum contract 뒤 independently decisive test가 있으면 병렬로 진행한다. 실제 producer 사실을 소비하는 integration acceptance는 real seam 검증 전 완료할 수 없다.

## 최소 capability의 필수 내용

`minimum_capability`에는 contract owner, public surface, request/event, response/projection, not-found/error semantics, consistency/transaction semantics, verification plan을 모두 기록한다. 이는 설계도가 아니라 worker가 구현·검증할 수 있는 최소 integration handoff다.

## 예시

### Seller → Catalog

Catalog가 필요한 사실은 “이 사용자가 ACTIVE Seller인가?”뿐이다. Seller 등록·심사·프로필 API 전체는 선행 조건이 아니다.

```text
owner: Seller
public surface: SellerQueryApi.isActiveSeller(userId)
response: boolean
error/consistency: read-only current SellerStatus 조회
verification: SellerQueryApi test + CatalogSellerAdapter integration test
```

### P9 Offer → P3 Cart

Cart가 필요한 사실은 Offer 존재, 판매 가능 상태, 현재 적용 가격, 최대 구매 수량, 구매 가능 재고와 OFFER-001 의미다. Seller CRUD, Offer 등록 HTTP API, 재고 차감·예약, Marketplace 전체 구현은 최소 capability가 아니다.

```text
owner: P9 Offer
public surface: OfferQueryApi.findCurrentOffer(offerId)
response: OfferSnapshot(offerId, price, currency, saleStatus, maxPurchaseQuantity, availableQuantity)
error/consistency: OFFER-001, inactive/archived/out-of-stock 의미, read-only snapshot
verification: P9 producer test + P3 adapter test + real producer-consumer integration test
```

상품명·썸네일·Variant 표시 정보는 P9 Offer가 아니라 P2 Catalog 소유라면 별도 Catalog query seam으로 분리한다.

### P3 Cart → P11 Auth / P5 Order

이 경우 Cart는 consumer가 아니라 producer다. P11은 로그인 후 guest Cart 병합을 호출하고, P5는 결제 성공 뒤 선택 CartItem 정리를 호출한다. P11/P5 내부 구현을 선행 조건으로 삼지 말고 Cart가 제공할 merge/cleanup public contract를 별도 card에서 구현·검증한다.

## 금지 사항

- 승인된 target contract를 실제 구현된 `published` contract로 기록하지 않는다.
- `not-applicable`을 “producer 내부를 바꾸지 않는다”는 이유로 사용하지 않는다.
- consumer-owned port, always-empty adapter, fixture/mock만으로 actual producer integration 완료를 주장하지 않는다.
- consumer가 producer 전체 구현을 자동으로 기다리게 하지 않는다. 다만 real seam을 요구하는 integration acceptance는 producer bridge 검증 뒤에만 완료한다.
