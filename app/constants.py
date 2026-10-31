class Role:
    CUSTOMER = "CUSTOMER"
    DRIVER = "DRIVER"
    ADMIN = "ADMIN"
    SUPER_ADMIN = "SUPER_ADMIN"
    ADMINS = {"ADMIN", "SUPER_ADMIN"}


class AccountStatus:
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    NEEDS_CORRECTION = "NEEDS_CORRECTION"
    SUSPENDED = "SUSPENDED"
    DELETED = "DELETED"  # user deleted their account; anonymized after the grace period
    BANNED = "BANNED"  # permanent: cannot be reactivated, CPR/phone cannot register again


class ServiceType:
    PERSON = "PERSON"
    DELIVERY = "DELIVERY"


class OrderStatus:
    PENDING = "PENDING"
    OFFERED = "OFFERED"
    NO_DRIVER_AVAILABLE = "NO_DRIVER_AVAILABLE"
    ACCEPTED = "ACCEPTED"
    DRIVER_ON_THE_WAY = "DRIVER_ON_THE_WAY"
    ARRIVED_AT_PICKUP = "ARRIVED_AT_PICKUP"
    IN_PROGRESS = "IN_PROGRESS"
    ARRIVED_AT_DESTINATION = "ARRIVED_AT_DESTINATION"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"

    FINAL = {COMPLETED, CANCELLED, EXPIRED}
    SEARCHING = {PENDING, OFFERED, NO_DRIVER_AVAILABLE}
    DRIVER_BUSY = {ACCEPTED, DRIVER_ON_THE_WAY, ARRIVED_AT_PICKUP, IN_PROGRESS, ARRIVED_AT_DESTINATION}


class OfferStatus:
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    DECLINED = "DECLINED"
    EXPIRED = "EXPIRED"  # shows in the driver's "unaccepted" list
    CLOSED = "CLOSED"  # order taken by someone else / cancelled


class ReviewStatus:
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


# Supported currencies -> number of minor-unit decimals (ISO 4217). Add more here if needed.
CURRENCIES = {"BHD": 3, "KWD": 3, "OMR": 3, "JOD": 3, "SAR": 2, "AED": 2, "QAR": 2, "EGP": 2,
              "USD": 2, "EUR": 2, "GBP": 2, "JPY": 0}
DEFAULT_CURRENCY = "BHD"
