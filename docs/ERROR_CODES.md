# Wasaly API error codes

Every error response: `{"detail": ..., "code": "STABLE_CODE", "status": <http status>, "message": "English sentence"}`. Messages are English; clients translate by `code`.

| Code | Defined in |
|---|---|
| ACCOUNT_ | routers/phoenix_id.py |
| ACCOUNT_BANNED | routers/auth.py |
| ACCOUNT_DELETED | routers/auth.py, routers/v16.py |
| ACCOUNT_NOT_APPROVED | deps.py |
| ACCOUNT_NOT_ELIGIBLE_FOR_VERIFICATION | routers/auth.py |
| ACTION_ALREADY_DONE | services/chat_agent.py |
| ACTION_EXPIRED | services/chat_agent.py |
| ACTION_INVALID | services/chat_agent.py |
| ACTION_NOT_FOUND | services/chat_agent.py |
| ACTIVE_ORDER_EXISTS | routers/account_deletion.py, routers/driver.py |
| ACTIVE_SUBSCRIPTION_EXISTS | routers/account_deletion.py |
| ADJUST_AFTER_PAYMENT | routers/admin/orders.py |
| AI_CUSTOMER_DOCUMENT_CORRECTION_ONLY | routers/admin/changes.py |
| AI_PERMISSION_REQUIRED | routers/chat.py |
| AI_PROVIDER_ERROR | routers/chat.py |
| AI_REVIEW_ONLY_PENDING_USERS | routers/admin/users.py |
| AI_TIMEOUT | routers/chat.py |
| AI_UNAVAILABLE | routers/chat.py |
| ALREADY_APPROVED | routers/services_v30.py |
| ALREADY_DRIVER | routers/v15.py |
| ALREADY_FINAL | routers/control.py |
| ALREADY_LINKED | routers/phoenix_id.py |
| ALREADY_PAID | routers/v20.py |
| ALREADY_RATED | routers/ratings.py |
| AMOUNT_ZERO | routers/v20.py |
| APPLICATION_PENDING | routers/v15.py |
| AREAS_REQUIRED | routers/driver.py |
| AREA_IN_USE | routers/v28.py |
| AVATAR_LOCKED | routers/profile.py |
| BAD_AMOUNT | routers/v20.py |
| BAD_PERIOD | routers/v20.py |
| BAD_REDIRECT_URI | routers/phoenix_id.py |
| BAD_TYPE | routers/v21.py |
| BOOLEAN_EXPECTED | routers/control.py |
| CANCEL_NOT_ALLOWED | routers/orders.py |
| CANNOT_CANCEL | routers/merchant_api.py |
| CANNOT_CANCEL_NOW | routers/driver.py |
| CANNOT_GO_ONLINE | routers/driver.py |
| CANNOT_WORK | routers/driver.py |
| CHALLENGE_EXPIRED | routers/v16.py |
| CHAT_ROLE_NOT_ALLOWED | routers/chat.py |
| COMMISSION_PERCENT_TOO_HIGH | routers/admin/settings.py |
| COMMISSION_REQUIRED_WITH_CURRENCY_CHANGE | routers/admin/settings.py |
| COMPANY_NOT_VERIFIED | routers/corporate.py |
| CONFIRM_REQUIRED | routers/v20.py |
| CONVERSATION_NOT_FOUND | routers/chat.py |
| CORPORATE_LIMIT_REACHED | routers/corporate.py |
| CORPORATE_MANAGER_ONLY | routers/corporate.py |
| CORRECTION_NOT_OPEN | routers/corrections.py |
| COUNTER_EXISTS | services/price_increase.py |
| COUNTER_EXPIRED | routers/orders.py |
| COUNTER_NOT_PENDING | routers/orders.py |
| CPR_DATES_REQUIRED | routers/profile.py |
| CPR_EXISTS | routers/admin/edits.py, routers/auth.py, routers/profile.py |
| CPR_MOVED_TO_PHOENIX_ID | routers/profile.py |
| CUSTOMER_TO_DRIVER_DISABLED | routers/v15.py |
| DEBT_OUTSTANDING | routers/account_deletion.py |
| DELIVERY_NOT_FOUND | routers/merchant_api.py, routers/portal.py |
| DRIVER_BUSY | routers/orders.py |
| DRIVER_NOT_ELIGIBLE | routers/services_v30.py |
| EMERGENCY_LOCKDOWN | services/modes.py |
| EQUIPMENT_NOT_AVAILABLE | routers/services_v30.py |
| ERROR | routers/v28.py |
| EXPIRY_IN_PAST | routers/v14.py |
| FAMILY | services/settings_service.py |
| FEE_CHANGED | routers/merchant_api.py |
| FEE_PAYER_REQUIRED | routers/merchant_api.py |
| FIELD_REQUIRED | routers/control.py |
| FLAG_ALREADY_RESOLVED | routers/v24.py |
| FLAG_NOT_FOUND | routers/v24.py |
| FORBIDDEN | routers/v11.py |
| IMPERSONATION_NOT_SUPPORTED | routers/control.py |
| INSUFFICIENT_WALLET | routers/v20.py |
| INVALID_CARD_TYPE | routers/payments.py |
| INVALID_CPR_DATES | routers/auth.py |
| INVALID_DATE | routers/app_errors.py |
| INVALID_DATES | routers/auth.py, routers/v14.py, routers/v15.py |
| INVALID_FIELD | routers/admin/changes.py, services/corrections.py |
| INVALID_FILE_KEY | routers/admin/edits.py, routers/driver.py |
| INVALID_MERCHANT_KEY | routers/merchant_api.py |
| INVALID_PASSWORD | routers/v28.py |
| INVALID_PERIOD | routers/v27.py |
| INVALID_PRICING_VALUE | routers/services_v30.py |
| INVALID_REQUEST | routers/merchant_api.py |
| INVALID_TOKEN | routers/phoenix_id.py |
| INVALID_TRANSITION | services/orders.py |
| INVALID_VALUE | services/corrections.py |
| INVALID_VEHICLE_CATEGORY | routers/services_v30.py |
| INVALID_WEBHOOK_JSON | routers/auth.py |
| INVALID_WEBHOOK_URL | routers/merchant_api.py |
| KNOWLEDGE_BASE_SAVE_FAILED | routers/chat.py |
| KNOWLEDGE_BASE_TOO_LARGE | routers/chat.py |
| KNOWLEDGE_BASE_UNAVAILABLE | routers/chat.py |
| KYC_REQUIRED | services/kyc.py |
| LABEL_TAKEN | routers/places.py |
| LICENSE_EXPIRED | routers/driver.py, routers/v15.py, services/corrections.py |
| LIVE_DISABLED | routers/driver.py |
| LIVE_TEST_TAP_DISABLED | services/live_test.py |
| LOCAL_LOGIN_DISABLED | routers/auth.py |
| LOCATION_CONSENT_REQUIRED | routers/activity.py, routers/driver.py |
| LOCATION_DISABLED | routers/activity.py |
| MAINTENANCE_MODE | routers/orders.py, routers/services_v30.py |
| MAX_STOPS | routers/v11.py |
| METHOD_DISABLED | routers/payments.py |
| MISSING_ITEMS | routers/app_errors.py, routers/corrections.py, services/corrections.py |
| NEEDS_NAME_AND_PASSWORD | routers/portal.py |
| NOTHING_TO_CHANGE | routers/profile.py |
| NOTHING_TO_RETRY | routers/v20.py |
| NOT_ACTIVATABLE | routers/control.py |
| NOT_APPLICABLE | routers/control.py |
| NOT_AWAITING_CONFIRMATION | routers/control.py, routers/payments.py |
| NOT_BANNED | routers/admin/users.py |
| NOT_CORPORATE_MEMBER | routers/corporate.py |
| NOT_DELETED | routers/account_deletion.py |
| NOT_FAVORITE_DRIVER | routers/orders.py |
| NOT_LIVE | routers/driver.py |
| NOT_PENDING | routers/v28.py |
| NOT_PENDING_REVIEW | routers/admin/changes.py, routers/v15.py |
| NOT_QUOTABLE | routers/subscriptions.py |
| NOT_REJECTABLE | routers/subscriptions.py |
| NOT_RENEWABLE | services/subscriptions.py |
| NOT_SCHEDULED | routers/subscriptions.py, routers/v11.py |
| NOT_TODAY | routers/subscriptions.py |
| NO_COORDINATES | routers/merchant_api.py |
| NO_SUBSCRIPTION | routers/control.py |
| NUMBER_EXPECTED | routers/control.py |
| OBJECT_EXPECTED | routers/control.py |
| OFFER_EXPIRED | services/price_increase.py |
| OFFER_EXPIRED_OR_UNAVAILABLE | routers/driver.py |
| OFFER_NOT_PENDING | services/price_increase.py |
| OPEN_ORDER_EXISTS | routers/orders.py, routers/services_v30.py |
| ORDER_ALREADY_FINAL | routers/admin/orders.py |
| ORDER_ALREADY_TAKEN | routers/driver.py |
| ORDER_NOT_ACTIVE | routers/orders.py, routers/v11.py |
| ORDER_NOT_ASSIGNABLE | routers/services_v30.py |
| ORDER_NOT_COMPLETED | routers/ratings.py |
| ORDER_NOT_FOUND | routers/live_order.py, routers/traffic.py |
| ORDER_NOT_SEARCHING | routers/orders.py, services/price_increase.py |
| OUTSIDE_SERVICE_AREA | routers/merchant_api.py, routers/orders.py, routers/services_v30.py |
| PARTNER_HAS_UNPAID | routers/v28.py |
| PARTNER_NO_BANK_DETAILS | routers/v20.py |
| PARTNER_PERCENT_EXCEEDS_100 | routers/v20.py |
| PASSKEYS_DISABLED | routers/v16.py |
| PASSKEYS_UNAVAILABLE | routers/v16.py |
| PASSKEY_EXISTS | routers/v16.py |
| PASSKEY_NOT_FOUND | routers/v16.py |
| PASSKEY_VERIFICATION_FAILED | routers/v16.py |
| PAYMENT_NOT_OPEN | routers/payments.py |
| PAYOUT_IN_PROGRESS | routers/v20.py |
| PENDING_USER_NOT_FOUND | routers/chat.py |
| PERMANENTLY_BANNED | routers/admin/users.py |
| PHOENIX_ID_UNREACHABLE | routers/phoenix_id.py |
| PHONE_ALREADY_VERIFIED | routers/auth.py |
| PHONE_EXISTS | routers/admin/edits.py, routers/auth.py, routers/corrections.py |
| PHONE_NOT_VERIFIED | routers/admin/users.py, routers/auth.py |
| PHONE_VERIFICATION_EXPIRED | routers/auth.py |
| PHONE_VERIFICATION_REQUIRED | routers/auth.py |
| PHONE_VERIFICATION_STATE_INVALID | routers/auth.py |
| PLACES_LIMIT | routers/places.py |
| PLANS_ARE_FIXED | routers/control.py |
| PLANS_ARE_GLOBAL | routers/control.py |
| PLATE_EXISTS | routers/admin/edits.py, routers/v15.py, services/corrections.py |
| PORTAL_ACCESS_DENIED | routers/portal.py |
| PRICES_REQUIRED_WITH_CURRENCY_CHANGE | routers/admin/settings.py |
| PRICE_INCREASE_DISABLED | services/price_increase.py |
| PRICING_NOT_CONFIGURED | routers/services_v30.py |
| PROMO_DISABLED | services/promo.py |
| PROMO_EXISTS | routers/promo.py |
| PROMO_EXPIRED | services/promo.py |
| PROMO_INVALID | services/promo.py |
| PROMO_MIN_ORDER | services/promo.py |
| PROMO_NOT_STARTED | services/promo.py |
| QUOTE_CHANGED | services/chat_agent.py |
| QUOTE_EXPIRED | routers/services_v30.py |
| QUOTE_NOT_AVAILABLE | routers/services_v30.py |
| QUOTE_NOT_FOUND | routers/services_v30.py |
| QUOTE_NOT_PRICEABLE | routers/services_v30.py |
| RAISE_NOT_HIGHER | services/price_increase.py |
| RAISE_TOO_HIGH | services/price_increase.py |
| RATE_LIMIT | routers/app_errors.py |
| RATE_LIMITED | routers/auth.py, routers/chat.py, routers/merchant_api.py |
| RATINGS_DISABLED | routers/ratings.py |
| RATING_WINDOW_CLOSED | routers/ratings.py |
| REASON_REQUIRED | routers/admin/changes.py |
| REFERRAL_ALREADY_USED | services/promo.py |
| REFERRAL_INVALID | routers/v11.py |
| REFERRAL_LIMIT | routers/v11.py, services/promo.py |
| REFERRAL_SELF | routers/v11.py, services/promo.py |
| REFUND_NOT_SUPPORTED | routers/control.py |
| REINSTATEMENT_FEE_REQUIRED | routers/admin/users.py |
| RENEWAL_ALREADY_PENDING | routers/v14.py |
| RESTORE_WINDOW_CLOSED | routers/account_deletion.py |
| RETRY_LIMIT_REACHED | routers/orders.py |
| RETRY_NOT_ALLOWED | routers/orders.py |
| ROLE_NOT_ALLOWED | routers/phoenix_id.py |
| ROLE_NOT_GRANTED | routers/v15.py |
| ROLE_SWITCH_DISABLED | routers/v15.py |
| SAME_PHONE | routers/corrections.py |
| SAME_ROLE | routers/v15.py |
| SANDBOX_NOT_SUPPORTED | routers/merchant_api.py |
| SCHEDULED_DISABLED | routers/orders.py |
| SCHEDULE_TOO_FAR | routers/orders.py |
| SCHEDULE_TOO_SOON | routers/orders.py |
| SERVICE_DISABLED | routers/services_v30.py |
| SERVICE_NOT_FOUND | routers/services_v30.py |
| SHARE_DISABLED | routers/v11.py |
| SHARE_EXPIRED | routers/v11.py |
| SHARE_NOT_FOUND | routers/v11.py |
| SOS_DISABLED | routers/v11.py |
| SOS_NOT_FOUND | routers/v11.py |
| SOS_RESOLVED | routers/v11.py |
| SSO_DISABLED | routers/phoenix_id.py |
| STANDARD | services/settings_service.py |
| STOP_NOT_PENDING | routers/v11.py |
| SWITCH_BLOCKED_ACTIVE_ORDER | routers/v15.py |
| SWITCH_BLOCKED_ACTIVE_TRIP | routers/v15.py |
| TAP_BAD_RESPONSE | routers/payments.py |
| TAP_CHARGE_FAILED | services/tap.py |
| TAP_NOT_CONFIGURED | services/tap.py |
| TAP_PAYOUTS_NOT_CONFIGURED | routers/v20.py |
| TAP_RETRIEVE_FAILED | services/tap.py |
| TOO_MANY_DECIMALS | routers/admin/orders.py, routers/admin/settings.py |
| TOO_MANY_ITEMS | routers/app_errors.py |
| TRIP_ALREADY_STARTED | routers/orders.py |
| UNAUTHORIZED | deps.py, routers/account_deletion.py, routers/auth.py |
| UNKNOWN_ACTION | services/chat_agent.py |
| UNKNOWN_AREA | routers/driver.py |
| UNKNOWN_FIELD | routers/control.py |
| UNKNOWN_LIMITS | routers/control.py |
| UNKNOWN_MODE | routers/control.py |
| UNKNOWN_SETTINGS | routers/control.py |
| UNPAID_ORDER_EXISTS | routers/account_deletion.py, routers/orders.py |
| USER_NOT_FOUND | routers/corporate.py, routers/portal.py |
| VALIDATION_ERROR | main.py, routers/control.py, routers/ratings.py |
| VEHICLE_CLASS_UNAVAILABLE | routers/admin/edits.py, services/vehicle_classes.py |
| VEHICLE_NOT_FOUND | routers/driver.py |
| VEHICLE_TOO_SMALL | routers/services_v30.py |
| VIP | services/settings_service.py |
| WHATSAPP_REQUEST_RATE_LIMITED | routers/auth.py |
| WHATSAPP_VERIFICATION_NOT_CONFIGURED | routers/auth.py |
| WHATSAPP_VERIFICATION_NOT_FOUND | routers/auth.py |
| WHATSAPP_WEBHOOK_NOT_CONFIGURED | routers/auth.py |
| WHATSAPP_WEBHOOK_PROCESSING_FAILED | routers/auth.py |
| WHATSAPP_WEBHOOK_SIGNATURE_INVALID | routers/auth.py |
| WHATSAPP_WEBHOOK_VERIFICATION_FAILED | routers/auth.py |
