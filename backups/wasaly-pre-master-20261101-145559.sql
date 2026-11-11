--
-- PostgreSQL database dump
--

\restrict mtINAyWqzwdsYmMdMqWNhsFfbVhpgm9b7tBD3LY3tL6FFFvl2gYhv8tUphGicFU

-- Dumped from database version 16.15 (Debian 16.15-1.pgdg13+2)
-- Dumped by pg_dump version 16.15 (Debian 16.15-1.pgdg13+2)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: admin_audit_logs; Type: TABLE; Schema: public; Owner: transport
--

CREATE TABLE public.admin_audit_logs (
    id integer NOT NULL,
    admin_id integer,
    action character varying(60) NOT NULL,
    entity_type character varying(40) NOT NULL,
    entity_id character varying(40) NOT NULL,
    reason text,
    details json,
    created_at timestamp with time zone NOT NULL
);


ALTER TABLE public.admin_audit_logs OWNER TO transport;

--
-- Name: admin_audit_logs_id_seq; Type: SEQUENCE; Schema: public; Owner: transport
--

CREATE SEQUENCE public.admin_audit_logs_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.admin_audit_logs_id_seq OWNER TO transport;

--
-- Name: admin_audit_logs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: transport
--

ALTER SEQUENCE public.admin_audit_logs_id_seq OWNED BY public.admin_audit_logs.id;


--
-- Name: app_open_events; Type: TABLE; Schema: public; Owner: transport
--

CREATE TABLE public.app_open_events (
    id integer NOT NULL,
    user_id integer NOT NULL,
    device_id character varying(100),
    app_version character varying(30),
    created_at timestamp with time zone NOT NULL
);


ALTER TABLE public.app_open_events OWNER TO transport;

--
-- Name: app_open_events_id_seq; Type: SEQUENCE; Schema: public; Owner: transport
--

CREATE SEQUENCE public.app_open_events_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.app_open_events_id_seq OWNER TO transport;

--
-- Name: app_open_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: transport
--

ALTER SEQUENCE public.app_open_events_id_seq OWNED BY public.app_open_events.id;


--
-- Name: app_settings; Type: TABLE; Schema: public; Owner: transport
--

CREATE TABLE public.app_settings (
    key character varying(60) NOT NULL,
    value text NOT NULL,
    updated_by integer,
    updated_at timestamp with time zone NOT NULL
);


ALTER TABLE public.app_settings OWNER TO transport;

--
-- Name: complaints; Type: TABLE; Schema: public; Owner: transport
--

CREATE TABLE public.complaints (
    id integer NOT NULL,
    user_id integer,
    order_id integer,
    type character varying(30) NOT NULL,
    subject character varying(200) NOT NULL,
    message text NOT NULL,
    status character varying(20) NOT NULL,
    admin_response text,
    resolved_by integer,
    created_at timestamp with time zone NOT NULL,
    resolved_at timestamp with time zone
);


ALTER TABLE public.complaints OWNER TO transport;

--
-- Name: complaints_id_seq; Type: SEQUENCE; Schema: public; Owner: transport
--

CREATE SEQUENCE public.complaints_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.complaints_id_seq OWNER TO transport;

--
-- Name: complaints_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: transport
--

ALTER SEQUENCE public.complaints_id_seq OWNED BY public.complaints.id;


--
-- Name: device_tokens; Type: TABLE; Schema: public; Owner: transport
--

CREATE TABLE public.device_tokens (
    id integer NOT NULL,
    user_id integer NOT NULL,
    token character varying(300) NOT NULL,
    platform character varying(20) NOT NULL,
    created_at timestamp with time zone NOT NULL
);


ALTER TABLE public.device_tokens OWNER TO transport;

--
-- Name: device_tokens_id_seq; Type: SEQUENCE; Schema: public; Owner: transport
--

CREATE SEQUENCE public.device_tokens_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.device_tokens_id_seq OWNER TO transport;

--
-- Name: device_tokens_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: transport
--

ALTER SEQUENCE public.device_tokens_id_seq OWNED BY public.device_tokens.id;


--
-- Name: driver_payouts; Type: TABLE; Schema: public; Owner: transport
--

CREATE TABLE public.driver_payouts (
    id integer NOT NULL,
    payment_id integer NOT NULL,
    driver_id integer NOT NULL,
    amount numeric(12,3) NOT NULL,
    debt_offset numeric(12,3) NOT NULL,
    currency character varying(3) NOT NULL,
    currency_decimals integer NOT NULL,
    status character varying(15) NOT NULL,
    simulated boolean NOT NULL,
    reference character varying(120),
    failure_reason text,
    created_at timestamp with time zone NOT NULL,
    transferred_at timestamp with time zone
);


ALTER TABLE public.driver_payouts OWNER TO transport;

--
-- Name: driver_payouts_id_seq; Type: SEQUENCE; Schema: public; Owner: transport
--

CREATE SEQUENCE public.driver_payouts_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.driver_payouts_id_seq OWNER TO transport;

--
-- Name: driver_payouts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: transport
--

ALTER SEQUENCE public.driver_payouts_id_seq OWNED BY public.driver_payouts.id;


--
-- Name: driver_profiles; Type: TABLE; Schema: public; Owner: transport
--

CREATE TABLE public.driver_profiles (
    debt_balance numeric(12,3) NOT NULL,
    id integer NOT NULL,
    user_id integer NOT NULL,
    license_number_encrypted text NOT NULL,
    license_expiry_date date NOT NULL,
    license_document character varying(255),
    driver_status character varying(30) NOT NULL,
    payout_method character varying(20),
    payout_account_encrypted text,
    payout_holder_name character varying(120),
    is_online boolean NOT NULL,
    mode character varying(10) NOT NULL,
    last_lat double precision,
    last_lng double precision,
    last_location_at timestamp with time zone,
    last_speed_kmh double precision,
    last_heading double precision,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


ALTER TABLE public.driver_profiles OWNER TO transport;

--
-- Name: driver_profiles_id_seq; Type: SEQUENCE; Schema: public; Owner: transport
--

CREATE SEQUENCE public.driver_profiles_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.driver_profiles_id_seq OWNER TO transport;

--
-- Name: driver_profiles_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: transport
--

ALTER SEQUENCE public.driver_profiles_id_seq OWNED BY public.driver_profiles.id;


--
-- Name: driver_service_areas; Type: TABLE; Schema: public; Owner: transport
--

CREATE TABLE public.driver_service_areas (
    driver_id integer NOT NULL,
    service_area_id integer NOT NULL
);


ALTER TABLE public.driver_service_areas OWNER TO transport;

--
-- Name: driver_work_sessions; Type: TABLE; Schema: public; Owner: transport
--

CREATE TABLE public.driver_work_sessions (
    id integer NOT NULL,
    driver_id integer NOT NULL,
    online_at timestamp with time zone NOT NULL,
    offline_at timestamp with time zone
);


ALTER TABLE public.driver_work_sessions OWNER TO transport;

--
-- Name: driver_work_sessions_id_seq; Type: SEQUENCE; Schema: public; Owner: transport
--

CREATE SEQUENCE public.driver_work_sessions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.driver_work_sessions_id_seq OWNER TO transport;

--
-- Name: driver_work_sessions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: transport
--

ALTER SEQUENCE public.driver_work_sessions_id_seq OWNED BY public.driver_work_sessions.id;


--
-- Name: location_snapshots; Type: TABLE; Schema: public; Owner: transport
--

CREATE TABLE public.location_snapshots (
    id integer NOT NULL,
    user_id integer NOT NULL,
    event character varying(30) NOT NULL,
    latitude double precision NOT NULL,
    longitude double precision NOT NULL,
    accuracy_m double precision,
    order_id integer,
    created_at timestamp with time zone NOT NULL
);


ALTER TABLE public.location_snapshots OWNER TO transport;

--
-- Name: location_snapshots_id_seq; Type: SEQUENCE; Schema: public; Owner: transport
--

CREATE SEQUENCE public.location_snapshots_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.location_snapshots_id_seq OWNER TO transport;

--
-- Name: location_snapshots_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: transport
--

ALTER SEQUENCE public.location_snapshots_id_seq OWNED BY public.location_snapshots.id;


--
-- Name: login_events; Type: TABLE; Schema: public; Owner: transport
--

CREATE TABLE public.login_events (
    id integer NOT NULL,
    user_id integer NOT NULL,
    success boolean NOT NULL,
    ip character varying(64),
    user_agent character varying(300),
    device_id character varying(100),
    new_device boolean NOT NULL,
    created_at timestamp with time zone NOT NULL
);


ALTER TABLE public.login_events OWNER TO transport;

--
-- Name: login_events_id_seq; Type: SEQUENCE; Schema: public; Owner: transport
--

CREATE SEQUENCE public.login_events_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.login_events_id_seq OWNER TO transport;

--
-- Name: login_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: transport
--

ALTER SEQUENCE public.login_events_id_seq OWNED BY public.login_events.id;


--
-- Name: notifications; Type: TABLE; Schema: public; Owner: transport
--

CREATE TABLE public.notifications (
    id integer NOT NULL,
    user_id integer NOT NULL,
    notification_type character varying(40) NOT NULL,
    title character varying(150) NOT NULL,
    message text NOT NULL,
    related_order_id integer,
    is_read boolean NOT NULL,
    created_at timestamp with time zone NOT NULL
);


ALTER TABLE public.notifications OWNER TO transport;

--
-- Name: notifications_id_seq; Type: SEQUENCE; Schema: public; Owner: transport
--

CREATE SEQUENCE public.notifications_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.notifications_id_seq OWNER TO transport;

--
-- Name: notifications_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: transport
--

ALTER SEQUENCE public.notifications_id_seq OWNED BY public.notifications.id;


--
-- Name: order_offers; Type: TABLE; Schema: public; Owner: transport
--

CREATE TABLE public.order_offers (
    id integer NOT NULL,
    order_id integer NOT NULL,
    driver_id integer NOT NULL,
    offered_at timestamp with time zone NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    offer_status character varying(20) NOT NULL,
    responded_at timestamp with time zone
);


ALTER TABLE public.order_offers OWNER TO transport;

--
-- Name: order_offers_id_seq; Type: SEQUENCE; Schema: public; Owner: transport
--

CREATE SEQUENCE public.order_offers_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.order_offers_id_seq OWNER TO transport;

--
-- Name: order_offers_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: transport
--

ALTER SEQUENCE public.order_offers_id_seq OWNED BY public.order_offers.id;


--
-- Name: order_status_history; Type: TABLE; Schema: public; Owner: transport
--

CREATE TABLE public.order_status_history (
    id integer NOT NULL,
    order_id integer NOT NULL,
    old_status character varying(30),
    new_status character varying(30) NOT NULL,
    changed_by integer,
    changed_by_role character varying(20) NOT NULL,
    changed_at timestamp with time zone NOT NULL,
    notes text
);


ALTER TABLE public.order_status_history OWNER TO transport;

--
-- Name: order_status_history_id_seq; Type: SEQUENCE; Schema: public; Owner: transport
--

CREATE SEQUENCE public.order_status_history_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.order_status_history_id_seq OWNER TO transport;

--
-- Name: order_status_history_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: transport
--

ALTER SEQUENCE public.order_status_history_id_seq OWNED BY public.order_status_history.id;


--
-- Name: orders; Type: TABLE; Schema: public; Owner: transport
--

CREATE TABLE public.orders (
    id integer NOT NULL,
    customer_id integer NOT NULL,
    driver_id integer,
    service_area_id integer,
    client_request_id character varying(64),
    service_type character varying(20) NOT NULL,
    pickup_address character varying(300) NOT NULL,
    pickup_latitude double precision,
    pickup_longitude double precision,
    destination_address character varying(300) NOT NULL,
    destination_latitude double precision,
    destination_longitude double precision,
    contact_phone character varying(20),
    passenger_count integer,
    luggage_count integer,
    cargo_description text,
    cargo_quantity integer,
    cargo_size_note character varying(200),
    cargo_weight_kg numeric(8,2),
    cargo_photo character varying(255),
    receiver_name character varying(120),
    receiver_phone character varying(20),
    driver_instructions text,
    customer_notes text,
    order_status character varying(30) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    accepted_at timestamp with time zone,
    driver_on_the_way_at timestamp with time zone,
    driver_arrived_at timestamp with time zone,
    trip_started_at timestamp with time zone,
    trip_arrived_at timestamp with time zone,
    actual_duration_seconds integer,
    currency character varying(3) NOT NULL,
    currency_decimals integer NOT NULL,
    price_per_unit numeric(12,3) NOT NULL,
    unit_duration_seconds integer NOT NULL,
    min_price numeric(12,3) NOT NULL,
    final_price numeric(12,3),
    base_price numeric(12,3),
    night_enabled boolean NOT NULL,
    night_percent numeric(6,2) NOT NULL,
    night_start character varying(5) NOT NULL,
    night_end character varying(5) NOT NULL,
    night_applied boolean NOT NULL,
    night_surcharge numeric(12,3),
    retry_count integer NOT NULL,
    est_distance_km double precision,
    est_duration_min integer,
    is_outside_bahrain boolean NOT NULL,
    outside_driver_percent numeric(6,2) NOT NULL,
    outside_driver_cut numeric(12,3),
    commission_type character varying(10) NOT NULL,
    commission_value numeric(12,3) NOT NULL,
    commission_mode character varying(10) NOT NULL,
    commission numeric(12,3),
    customer_total numeric(12,3),
    driver_earning numeric(12,3),
    original_price numeric(12,3),
    price_adjusted boolean NOT NULL,
    payment_id integer,
    payment_method character varying(20),
    payment_status character varying(24) NOT NULL,
    completed_at timestamp with time zone,
    cancelled_at timestamp with time zone,
    cancelled_by integer,
    cancelled_by_role character varying(20),
    cancellation_reason text
);


ALTER TABLE public.orders OWNER TO transport;

--
-- Name: orders_id_seq; Type: SEQUENCE; Schema: public; Owner: transport
--

CREATE SEQUENCE public.orders_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.orders_id_seq OWNER TO transport;

--
-- Name: orders_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: transport
--

ALTER SEQUENCE public.orders_id_seq OWNED BY public.orders.id;


--
-- Name: payments; Type: TABLE; Schema: public; Owner: transport
--

CREATE TABLE public.payments (
    id integer NOT NULL,
    kind character varying(15) NOT NULL,
    order_id integer,
    subscription_id integer,
    customer_id integer NOT NULL,
    driver_id integer,
    amount numeric(12,3) NOT NULL,
    currency character varying(3) NOT NULL,
    currency_decimals integer NOT NULL,
    driver_amount numeric(12,3) NOT NULL,
    company_amount numeric(12,3) NOT NULL,
    gateway_fee numeric(12,3) NOT NULL,
    charged_amount numeric(12,3),
    payment_method character varying(20),
    payment_status character varying(24) NOT NULL,
    transaction_reference character varying(100),
    tap_url character varying(500),
    proof_file character varying(255),
    reject_reason text,
    verified_by integer,
    paid_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


ALTER TABLE public.payments OWNER TO transport;

--
-- Name: payments_id_seq; Type: SEQUENCE; Schema: public; Owner: transport
--

CREATE SEQUENCE public.payments_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.payments_id_seq OWNER TO transport;

--
-- Name: payments_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: transport
--

ALTER SEQUENCE public.payments_id_seq OWNED BY public.payments.id;


--
-- Name: ratings; Type: TABLE; Schema: public; Owner: transport
--

CREATE TABLE public.ratings (
    id integer NOT NULL,
    order_id integer NOT NULL,
    rater_id integer NOT NULL,
    ratee_id integer NOT NULL,
    rater_role character varying(20) NOT NULL,
    stars integer NOT NULL,
    tags json NOT NULL,
    comment text,
    created_at timestamp with time zone NOT NULL
);


ALTER TABLE public.ratings OWNER TO transport;

--
-- Name: ratings_id_seq; Type: SEQUENCE; Schema: public; Owner: transport
--

CREATE SEQUENCE public.ratings_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.ratings_id_seq OWNER TO transport;

--
-- Name: ratings_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: transport
--

ALTER SEQUENCE public.ratings_id_seq OWNED BY public.ratings.id;


--
-- Name: service_areas; Type: TABLE; Schema: public; Owner: transport
--

CREATE TABLE public.service_areas (
    id integer NOT NULL,
    name character varying(100) NOT NULL,
    boundary json,
    is_active boolean NOT NULL
);


ALTER TABLE public.service_areas OWNER TO transport;

--
-- Name: service_areas_id_seq; Type: SEQUENCE; Schema: public; Owner: transport
--

CREATE SEQUENCE public.service_areas_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.service_areas_id_seq OWNER TO transport;

--
-- Name: service_areas_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: transport
--

ALTER SEQUENCE public.service_areas_id_seq OWNED BY public.service_areas.id;


--
-- Name: subscription_trips; Type: TABLE; Schema: public; Owner: transport
--

CREATE TABLE public.subscription_trips (
    id integer NOT NULL,
    subscription_id integer NOT NULL,
    driver_id integer,
    trip_date date NOT NULL,
    leg character varying(8) NOT NULL,
    scheduled_time character varying(5) NOT NULL,
    status character varying(12) NOT NULL,
    completed_at timestamp with time zone,
    note text
);


ALTER TABLE public.subscription_trips OWNER TO transport;

--
-- Name: subscription_trips_id_seq; Type: SEQUENCE; Schema: public; Owner: transport
--

CREATE SEQUENCE public.subscription_trips_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.subscription_trips_id_seq OWNER TO transport;

--
-- Name: subscription_trips_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: transport
--

ALTER SEQUENCE public.subscription_trips_id_seq OWNED BY public.subscription_trips.id;


--
-- Name: subscriptions; Type: TABLE; Schema: public; Owner: transport
--

CREATE TABLE public.subscriptions (
    id integer NOT NULL,
    customer_id integer NOT NULL,
    driver_id integer,
    service_area_id integer,
    category character varying(15) NOT NULL,
    passenger_name character varying(120),
    school_name character varying(160),
    passengers_count integer NOT NULL,
    pickup_address character varying(300) NOT NULL,
    pickup_latitude double precision,
    pickup_longitude double precision,
    destination_address character varying(300) NOT NULL,
    destination_latitude double precision,
    destination_longitude double precision,
    weekdays json NOT NULL,
    pickup_time character varying(5) NOT NULL,
    return_trip boolean NOT NULL,
    return_time character varying(5),
    notes text,
    status character varying(15) NOT NULL,
    currency character varying(3) NOT NULL,
    currency_decimals integer NOT NULL,
    monthly_price numeric(12,3),
    commission_type character varying(10) NOT NULL,
    commission_value numeric(12,3) NOT NULL,
    commission_mode character varying(10) NOT NULL,
    customer_total numeric(12,3),
    driver_earning numeric(12,3),
    commission numeric(12,3),
    paid_until date,
    starts_on date,
    reject_reason text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


ALTER TABLE public.subscriptions OWNER TO transport;

--
-- Name: subscriptions_id_seq; Type: SEQUENCE; Schema: public; Owner: transport
--

CREATE SEQUENCE public.subscriptions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.subscriptions_id_seq OWNER TO transport;

--
-- Name: subscriptions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: transport
--

ALTER SEQUENCE public.subscriptions_id_seq OWNED BY public.subscriptions.id;


--
-- Name: uploaded_files; Type: TABLE; Schema: public; Owner: transport
--

CREATE TABLE public.uploaded_files (
    file_key character varying(120) NOT NULL,
    owner_id integer,
    original_name character varying(200) NOT NULL,
    content_type character varying(80) NOT NULL,
    size integer NOT NULL,
    created_at timestamp with time zone NOT NULL
);


ALTER TABLE public.uploaded_files OWNER TO transport;

--
-- Name: user_devices; Type: TABLE; Schema: public; Owner: transport
--

CREATE TABLE public.user_devices (
    id integer NOT NULL,
    user_id integer NOT NULL,
    device_id character varying(100) NOT NULL,
    platform character varying(20) NOT NULL,
    model character varying(120),
    os_name character varying(40),
    os_version character varying(40),
    app_version character varying(30),
    locale character varying(20),
    last_ip character varying(64),
    open_count integer NOT NULL,
    first_seen_at timestamp with time zone NOT NULL,
    last_seen_at timestamp with time zone NOT NULL
);


ALTER TABLE public.user_devices OWNER TO transport;

--
-- Name: user_devices_id_seq; Type: SEQUENCE; Schema: public; Owner: transport
--

CREATE SEQUENCE public.user_devices_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.user_devices_id_seq OWNER TO transport;

--
-- Name: user_devices_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: transport
--

ALTER SEQUENCE public.user_devices_id_seq OWNED BY public.user_devices.id;


--
-- Name: users; Type: TABLE; Schema: public; Owner: transport
--

CREATE TABLE public.users (
    id integer NOT NULL,
    full_name character varying(120) NOT NULL,
    phone character varying(20) NOT NULL,
    cpr_encrypted text,
    cpr_hash character varying(64),
    cpr_last3 character varying(3),
    password_hash character varying(255) NOT NULL,
    role character varying(20) NOT NULL,
    account_status character varying(30) NOT NULL,
    status_reason text,
    profile_photo character varying(255),
    cpr_photo character varying(255),
    must_change_password boolean NOT NULL,
    token_version integer NOT NULL,
    failed_logins integer NOT NULL,
    locked_until timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    approved_at timestamp with time zone,
    approved_by integer,
    last_seen_at timestamp with time zone,
    last_login_at timestamp with time zone,
    last_login_ip character varying(64),
    login_count integer NOT NULL,
    app_open_count integer NOT NULL,
    auto_suspended boolean NOT NULL,
    auto_suspend_count integer NOT NULL,
    rating_reset_at timestamp with time zone,
    location_consent boolean NOT NULL,
    location_consent_at timestamp with time zone
);


ALTER TABLE public.users OWNER TO transport;

--
-- Name: users_id_seq; Type: SEQUENCE; Schema: public; Owner: transport
--

CREATE SEQUENCE public.users_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.users_id_seq OWNER TO transport;

--
-- Name: users_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: transport
--

ALTER SEQUENCE public.users_id_seq OWNED BY public.users.id;


--
-- Name: vehicle_documents; Type: TABLE; Schema: public; Owner: transport
--

CREATE TABLE public.vehicle_documents (
    id integer NOT NULL,
    vehicle_id integer NOT NULL,
    document_type character varying(40) NOT NULL,
    file_path character varying(255) NOT NULL,
    expiry_date date,
    review_status character varying(20) NOT NULL,
    review_reason text,
    reviewed_by integer,
    reviewed_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL
);


ALTER TABLE public.vehicle_documents OWNER TO transport;

--
-- Name: vehicle_documents_id_seq; Type: SEQUENCE; Schema: public; Owner: transport
--

CREATE SEQUENCE public.vehicle_documents_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.vehicle_documents_id_seq OWNER TO transport;

--
-- Name: vehicle_documents_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: transport
--

ALTER SEQUENCE public.vehicle_documents_id_seq OWNED BY public.vehicle_documents.id;


--
-- Name: vehicles; Type: TABLE; Schema: public; Owner: transport
--

CREATE TABLE public.vehicles (
    id integer NOT NULL,
    driver_id integer NOT NULL,
    make character varying(60) NOT NULL,
    model character varying(60) NOT NULL,
    year integer NOT NULL,
    color character varying(30) NOT NULL,
    plate_number character varying(20) NOT NULL,
    vehicle_type character varying(30) NOT NULL,
    passenger_capacity integer NOT NULL,
    luggage_capacity integer NOT NULL,
    cargo_capacity_kg numeric(8,2),
    has_ac boolean NOT NULL,
    is_comfortable boolean NOT NULL,
    photos json NOT NULL,
    vehicle_status character varying(30) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


ALTER TABLE public.vehicles OWNER TO transport;

--
-- Name: vehicles_id_seq; Type: SEQUENCE; Schema: public; Owner: transport
--

CREATE SEQUENCE public.vehicles_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.vehicles_id_seq OWNER TO transport;

--
-- Name: vehicles_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: transport
--

ALTER SEQUENCE public.vehicles_id_seq OWNED BY public.vehicles.id;


--
-- Name: admin_audit_logs id; Type: DEFAULT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.admin_audit_logs ALTER COLUMN id SET DEFAULT nextval('public.admin_audit_logs_id_seq'::regclass);


--
-- Name: app_open_events id; Type: DEFAULT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.app_open_events ALTER COLUMN id SET DEFAULT nextval('public.app_open_events_id_seq'::regclass);


--
-- Name: complaints id; Type: DEFAULT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.complaints ALTER COLUMN id SET DEFAULT nextval('public.complaints_id_seq'::regclass);


--
-- Name: device_tokens id; Type: DEFAULT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.device_tokens ALTER COLUMN id SET DEFAULT nextval('public.device_tokens_id_seq'::regclass);


--
-- Name: driver_payouts id; Type: DEFAULT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.driver_payouts ALTER COLUMN id SET DEFAULT nextval('public.driver_payouts_id_seq'::regclass);


--
-- Name: driver_profiles id; Type: DEFAULT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.driver_profiles ALTER COLUMN id SET DEFAULT nextval('public.driver_profiles_id_seq'::regclass);


--
-- Name: driver_work_sessions id; Type: DEFAULT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.driver_work_sessions ALTER COLUMN id SET DEFAULT nextval('public.driver_work_sessions_id_seq'::regclass);


--
-- Name: location_snapshots id; Type: DEFAULT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.location_snapshots ALTER COLUMN id SET DEFAULT nextval('public.location_snapshots_id_seq'::regclass);


--
-- Name: login_events id; Type: DEFAULT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.login_events ALTER COLUMN id SET DEFAULT nextval('public.login_events_id_seq'::regclass);


--
-- Name: notifications id; Type: DEFAULT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.notifications ALTER COLUMN id SET DEFAULT nextval('public.notifications_id_seq'::regclass);


--
-- Name: order_offers id; Type: DEFAULT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.order_offers ALTER COLUMN id SET DEFAULT nextval('public.order_offers_id_seq'::regclass);


--
-- Name: order_status_history id; Type: DEFAULT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.order_status_history ALTER COLUMN id SET DEFAULT nextval('public.order_status_history_id_seq'::regclass);


--
-- Name: orders id; Type: DEFAULT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.orders ALTER COLUMN id SET DEFAULT nextval('public.orders_id_seq'::regclass);


--
-- Name: payments id; Type: DEFAULT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.payments ALTER COLUMN id SET DEFAULT nextval('public.payments_id_seq'::regclass);


--
-- Name: ratings id; Type: DEFAULT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.ratings ALTER COLUMN id SET DEFAULT nextval('public.ratings_id_seq'::regclass);


--
-- Name: service_areas id; Type: DEFAULT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.service_areas ALTER COLUMN id SET DEFAULT nextval('public.service_areas_id_seq'::regclass);


--
-- Name: subscription_trips id; Type: DEFAULT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.subscription_trips ALTER COLUMN id SET DEFAULT nextval('public.subscription_trips_id_seq'::regclass);


--
-- Name: subscriptions id; Type: DEFAULT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.subscriptions ALTER COLUMN id SET DEFAULT nextval('public.subscriptions_id_seq'::regclass);


--
-- Name: user_devices id; Type: DEFAULT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.user_devices ALTER COLUMN id SET DEFAULT nextval('public.user_devices_id_seq'::regclass);


--
-- Name: users id; Type: DEFAULT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.users ALTER COLUMN id SET DEFAULT nextval('public.users_id_seq'::regclass);


--
-- Name: vehicle_documents id; Type: DEFAULT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.vehicle_documents ALTER COLUMN id SET DEFAULT nextval('public.vehicle_documents_id_seq'::regclass);


--
-- Name: vehicles id; Type: DEFAULT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.vehicles ALTER COLUMN id SET DEFAULT nextval('public.vehicles_id_seq'::regclass);


--
-- Data for Name: admin_audit_logs; Type: TABLE DATA; Schema: public; Owner: transport
--

COPY public.admin_audit_logs (id, admin_id, action, entity_type, entity_id, reason, details, created_at) FROM stdin;
\.


--
-- Data for Name: app_open_events; Type: TABLE DATA; Schema: public; Owner: transport
--

COPY public.app_open_events (id, user_id, device_id, app_version, created_at) FROM stdin;
\.


--
-- Data for Name: app_settings; Type: TABLE DATA; Schema: public; Owner: transport
--

COPY public.app_settings (key, value, updated_by, updated_at) FROM stdin;
\.


--
-- Data for Name: complaints; Type: TABLE DATA; Schema: public; Owner: transport
--

COPY public.complaints (id, user_id, order_id, type, subject, message, status, admin_response, resolved_by, created_at, resolved_at) FROM stdin;
\.


--
-- Data for Name: device_tokens; Type: TABLE DATA; Schema: public; Owner: transport
--

COPY public.device_tokens (id, user_id, token, platform, created_at) FROM stdin;
\.


--
-- Data for Name: driver_payouts; Type: TABLE DATA; Schema: public; Owner: transport
--

COPY public.driver_payouts (id, payment_id, driver_id, amount, debt_offset, currency, currency_decimals, status, simulated, reference, failure_reason, created_at, transferred_at) FROM stdin;
\.


--
-- Data for Name: driver_profiles; Type: TABLE DATA; Schema: public; Owner: transport
--

COPY public.driver_profiles (debt_balance, id, user_id, license_number_encrypted, license_expiry_date, license_document, driver_status, payout_method, payout_account_encrypted, payout_holder_name, is_online, mode, last_lat, last_lng, last_location_at, last_speed_kmh, last_heading, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: driver_service_areas; Type: TABLE DATA; Schema: public; Owner: transport
--

COPY public.driver_service_areas (driver_id, service_area_id) FROM stdin;
\.


--
-- Data for Name: driver_work_sessions; Type: TABLE DATA; Schema: public; Owner: transport
--

COPY public.driver_work_sessions (id, driver_id, online_at, offline_at) FROM stdin;
\.


--
-- Data for Name: location_snapshots; Type: TABLE DATA; Schema: public; Owner: transport
--

COPY public.location_snapshots (id, user_id, event, latitude, longitude, accuracy_m, order_id, created_at) FROM stdin;
\.


--
-- Data for Name: login_events; Type: TABLE DATA; Schema: public; Owner: transport
--

COPY public.login_events (id, user_id, success, ip, user_agent, device_id, new_device, created_at) FROM stdin;
1	1	f	46.184.210.41	Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.6.2 Safari/605.1.15	string	f	2026-11-01 14:17:40.37025+00
2	1	t	46.184.210.41	Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.6.2 Safari/605.1.15	string	t	2026-11-01 14:19:21.960224+00
3	1	t	46.184.210.41	Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.6.2 Safari/605.1.15	string	t	2026-11-01 14:30:11.122668+00
4	1	f	46.184.210.41	Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.6.2 Safari/605.1.15	string	f	2026-11-01 14:30:30.68307+00
5	1	t	46.184.210.41	Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.6.2 Safari/605.1.15	string	t	2026-11-01 14:31:50.879173+00
\.


--
-- Data for Name: notifications; Type: TABLE DATA; Schema: public; Owner: transport
--

COPY public.notifications (id, user_id, notification_type, title, message, related_order_id, is_read, created_at) FROM stdin;
\.


--
-- Data for Name: order_offers; Type: TABLE DATA; Schema: public; Owner: transport
--

COPY public.order_offers (id, order_id, driver_id, offered_at, expires_at, offer_status, responded_at) FROM stdin;
\.


--
-- Data for Name: order_status_history; Type: TABLE DATA; Schema: public; Owner: transport
--

COPY public.order_status_history (id, order_id, old_status, new_status, changed_by, changed_by_role, changed_at, notes) FROM stdin;
\.


--
-- Data for Name: orders; Type: TABLE DATA; Schema: public; Owner: transport
--

COPY public.orders (id, customer_id, driver_id, service_area_id, client_request_id, service_type, pickup_address, pickup_latitude, pickup_longitude, destination_address, destination_latitude, destination_longitude, contact_phone, passenger_count, luggage_count, cargo_description, cargo_quantity, cargo_size_note, cargo_weight_kg, cargo_photo, receiver_name, receiver_phone, driver_instructions, customer_notes, order_status, created_at, accepted_at, driver_on_the_way_at, driver_arrived_at, trip_started_at, trip_arrived_at, actual_duration_seconds, currency, currency_decimals, price_per_unit, unit_duration_seconds, min_price, final_price, base_price, night_enabled, night_percent, night_start, night_end, night_applied, night_surcharge, retry_count, est_distance_km, est_duration_min, is_outside_bahrain, outside_driver_percent, outside_driver_cut, commission_type, commission_value, commission_mode, commission, customer_total, driver_earning, original_price, price_adjusted, payment_id, payment_method, payment_status, completed_at, cancelled_at, cancelled_by, cancelled_by_role, cancellation_reason) FROM stdin;
\.


--
-- Data for Name: payments; Type: TABLE DATA; Schema: public; Owner: transport
--

COPY public.payments (id, kind, order_id, subscription_id, customer_id, driver_id, amount, currency, currency_decimals, driver_amount, company_amount, gateway_fee, charged_amount, payment_method, payment_status, transaction_reference, tap_url, proof_file, reject_reason, verified_by, paid_at, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: ratings; Type: TABLE DATA; Schema: public; Owner: transport
--

COPY public.ratings (id, order_id, rater_id, ratee_id, rater_role, stars, tags, comment, created_at) FROM stdin;
\.


--
-- Data for Name: service_areas; Type: TABLE DATA; Schema: public; Owner: transport
--

COPY public.service_areas (id, name, boundary, is_active) FROM stdin;
\.


--
-- Data for Name: subscription_trips; Type: TABLE DATA; Schema: public; Owner: transport
--

COPY public.subscription_trips (id, subscription_id, driver_id, trip_date, leg, scheduled_time, status, completed_at, note) FROM stdin;
\.


--
-- Data for Name: subscriptions; Type: TABLE DATA; Schema: public; Owner: transport
--

COPY public.subscriptions (id, customer_id, driver_id, service_area_id, category, passenger_name, school_name, passengers_count, pickup_address, pickup_latitude, pickup_longitude, destination_address, destination_latitude, destination_longitude, weekdays, pickup_time, return_trip, return_time, notes, status, currency, currency_decimals, monthly_price, commission_type, commission_value, commission_mode, customer_total, driver_earning, commission, paid_until, starts_on, reject_reason, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: uploaded_files; Type: TABLE DATA; Schema: public; Owner: transport
--

COPY public.uploaded_files (file_key, owner_id, original_name, content_type, size, created_at) FROM stdin;
\.


--
-- Data for Name: user_devices; Type: TABLE DATA; Schema: public; Owner: transport
--

COPY public.user_devices (id, user_id, device_id, platform, model, os_name, os_version, app_version, locale, last_ip, open_count, first_seen_at, last_seen_at) FROM stdin;
\.


--
-- Data for Name: users; Type: TABLE DATA; Schema: public; Owner: transport
--

COPY public.users (id, full_name, phone, cpr_encrypted, cpr_hash, cpr_last3, password_hash, role, account_status, status_reason, profile_photo, cpr_photo, must_change_password, token_version, failed_logins, locked_until, created_at, updated_at, approved_at, approved_by, last_seen_at, last_login_at, last_login_ip, login_count, app_open_count, auto_suspended, auto_suspend_count, rating_reset_at, location_consent, location_consent_at) FROM stdin;
1	Wasaly Super Admin	+97332251081	\N	\N	\N	$argon2id$v=19$m=65536,t=3,p=4$9+MUD1ssKQhoYrJ+mp4HAA$R51JwhXVSrSR65I2aLYf1I16nXQuw5cb1+fVb76Sguk	SUPER_ADMIN	APPROVED	\N	\N	\N	t	0	0	\N	2026-11-01 14:10:49.398885+00	2026-11-01 14:31:50.880089+00	2026-11-01 14:10:49.396126+00	\N	2026-11-01 14:31:50.878873+00	2026-11-01 14:31:50.878873+00	46.184.210.41	3	0	f	0	\N	f	\N
\.


--
-- Data for Name: vehicle_documents; Type: TABLE DATA; Schema: public; Owner: transport
--

COPY public.vehicle_documents (id, vehicle_id, document_type, file_path, expiry_date, review_status, review_reason, reviewed_by, reviewed_at, created_at) FROM stdin;
\.


--
-- Data for Name: vehicles; Type: TABLE DATA; Schema: public; Owner: transport
--

COPY public.vehicles (id, driver_id, make, model, year, color, plate_number, vehicle_type, passenger_capacity, luggage_capacity, cargo_capacity_kg, has_ac, is_comfortable, photos, vehicle_status, created_at, updated_at) FROM stdin;
\.


--
-- Name: admin_audit_logs_id_seq; Type: SEQUENCE SET; Schema: public; Owner: transport
--

SELECT pg_catalog.setval('public.admin_audit_logs_id_seq', 1, false);


--
-- Name: app_open_events_id_seq; Type: SEQUENCE SET; Schema: public; Owner: transport
--

SELECT pg_catalog.setval('public.app_open_events_id_seq', 1, false);


--
-- Name: complaints_id_seq; Type: SEQUENCE SET; Schema: public; Owner: transport
--

SELECT pg_catalog.setval('public.complaints_id_seq', 1, false);


--
-- Name: device_tokens_id_seq; Type: SEQUENCE SET; Schema: public; Owner: transport
--

SELECT pg_catalog.setval('public.device_tokens_id_seq', 1, false);


--
-- Name: driver_payouts_id_seq; Type: SEQUENCE SET; Schema: public; Owner: transport
--

SELECT pg_catalog.setval('public.driver_payouts_id_seq', 1, false);


--
-- Name: driver_profiles_id_seq; Type: SEQUENCE SET; Schema: public; Owner: transport
--

SELECT pg_catalog.setval('public.driver_profiles_id_seq', 1, false);


--
-- Name: driver_work_sessions_id_seq; Type: SEQUENCE SET; Schema: public; Owner: transport
--

SELECT pg_catalog.setval('public.driver_work_sessions_id_seq', 1, false);


--
-- Name: location_snapshots_id_seq; Type: SEQUENCE SET; Schema: public; Owner: transport
--

SELECT pg_catalog.setval('public.location_snapshots_id_seq', 1, false);


--
-- Name: login_events_id_seq; Type: SEQUENCE SET; Schema: public; Owner: transport
--

SELECT pg_catalog.setval('public.login_events_id_seq', 5, true);


--
-- Name: notifications_id_seq; Type: SEQUENCE SET; Schema: public; Owner: transport
--

SELECT pg_catalog.setval('public.notifications_id_seq', 1, false);


--
-- Name: order_offers_id_seq; Type: SEQUENCE SET; Schema: public; Owner: transport
--

SELECT pg_catalog.setval('public.order_offers_id_seq', 1, false);


--
-- Name: order_status_history_id_seq; Type: SEQUENCE SET; Schema: public; Owner: transport
--

SELECT pg_catalog.setval('public.order_status_history_id_seq', 1, false);


--
-- Name: orders_id_seq; Type: SEQUENCE SET; Schema: public; Owner: transport
--

SELECT pg_catalog.setval('public.orders_id_seq', 1, false);


--
-- Name: payments_id_seq; Type: SEQUENCE SET; Schema: public; Owner: transport
--

SELECT pg_catalog.setval('public.payments_id_seq', 1, false);


--
-- Name: ratings_id_seq; Type: SEQUENCE SET; Schema: public; Owner: transport
--

SELECT pg_catalog.setval('public.ratings_id_seq', 1, false);


--
-- Name: service_areas_id_seq; Type: SEQUENCE SET; Schema: public; Owner: transport
--

SELECT pg_catalog.setval('public.service_areas_id_seq', 1, false);


--
-- Name: subscription_trips_id_seq; Type: SEQUENCE SET; Schema: public; Owner: transport
--

SELECT pg_catalog.setval('public.subscription_trips_id_seq', 1, false);


--
-- Name: subscriptions_id_seq; Type: SEQUENCE SET; Schema: public; Owner: transport
--

SELECT pg_catalog.setval('public.subscriptions_id_seq', 1, false);


--
-- Name: user_devices_id_seq; Type: SEQUENCE SET; Schema: public; Owner: transport
--

SELECT pg_catalog.setval('public.user_devices_id_seq', 1, false);


--
-- Name: users_id_seq; Type: SEQUENCE SET; Schema: public; Owner: transport
--

SELECT pg_catalog.setval('public.users_id_seq', 1, true);


--
-- Name: vehicle_documents_id_seq; Type: SEQUENCE SET; Schema: public; Owner: transport
--

SELECT pg_catalog.setval('public.vehicle_documents_id_seq', 1, false);


--
-- Name: vehicles_id_seq; Type: SEQUENCE SET; Schema: public; Owner: transport
--

SELECT pg_catalog.setval('public.vehicles_id_seq', 1, false);


--
-- Name: admin_audit_logs admin_audit_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.admin_audit_logs
    ADD CONSTRAINT admin_audit_logs_pkey PRIMARY KEY (id);


--
-- Name: app_open_events app_open_events_pkey; Type: CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.app_open_events
    ADD CONSTRAINT app_open_events_pkey PRIMARY KEY (id);


--
-- Name: app_settings app_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.app_settings
    ADD CONSTRAINT app_settings_pkey PRIMARY KEY (key);


--
-- Name: complaints complaints_pkey; Type: CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.complaints
    ADD CONSTRAINT complaints_pkey PRIMARY KEY (id);


--
-- Name: device_tokens device_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.device_tokens
    ADD CONSTRAINT device_tokens_pkey PRIMARY KEY (id);


--
-- Name: device_tokens device_tokens_token_key; Type: CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.device_tokens
    ADD CONSTRAINT device_tokens_token_key UNIQUE (token);


--
-- Name: driver_payouts driver_payouts_payment_id_key; Type: CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.driver_payouts
    ADD CONSTRAINT driver_payouts_payment_id_key UNIQUE (payment_id);


--
-- Name: driver_payouts driver_payouts_pkey; Type: CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.driver_payouts
    ADD CONSTRAINT driver_payouts_pkey PRIMARY KEY (id);


--
-- Name: driver_profiles driver_profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.driver_profiles
    ADD CONSTRAINT driver_profiles_pkey PRIMARY KEY (id);


--
-- Name: driver_profiles driver_profiles_user_id_key; Type: CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.driver_profiles
    ADD CONSTRAINT driver_profiles_user_id_key UNIQUE (user_id);


--
-- Name: driver_service_areas driver_service_areas_pkey; Type: CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.driver_service_areas
    ADD CONSTRAINT driver_service_areas_pkey PRIMARY KEY (driver_id, service_area_id);


--
-- Name: driver_work_sessions driver_work_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.driver_work_sessions
    ADD CONSTRAINT driver_work_sessions_pkey PRIMARY KEY (id);


--
-- Name: location_snapshots location_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.location_snapshots
    ADD CONSTRAINT location_snapshots_pkey PRIMARY KEY (id);


--
-- Name: login_events login_events_pkey; Type: CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.login_events
    ADD CONSTRAINT login_events_pkey PRIMARY KEY (id);


--
-- Name: notifications notifications_pkey; Type: CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT notifications_pkey PRIMARY KEY (id);


--
-- Name: order_offers order_offers_pkey; Type: CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.order_offers
    ADD CONSTRAINT order_offers_pkey PRIMARY KEY (id);


--
-- Name: order_status_history order_status_history_pkey; Type: CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.order_status_history
    ADD CONSTRAINT order_status_history_pkey PRIMARY KEY (id);


--
-- Name: orders orders_pkey; Type: CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_pkey PRIMARY KEY (id);


--
-- Name: payments payments_pkey; Type: CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_pkey PRIMARY KEY (id);


--
-- Name: ratings ratings_pkey; Type: CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.ratings
    ADD CONSTRAINT ratings_pkey PRIMARY KEY (id);


--
-- Name: service_areas service_areas_name_key; Type: CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.service_areas
    ADD CONSTRAINT service_areas_name_key UNIQUE (name);


--
-- Name: service_areas service_areas_pkey; Type: CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.service_areas
    ADD CONSTRAINT service_areas_pkey PRIMARY KEY (id);


--
-- Name: subscription_trips subscription_trips_pkey; Type: CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.subscription_trips
    ADD CONSTRAINT subscription_trips_pkey PRIMARY KEY (id);


--
-- Name: subscriptions subscriptions_pkey; Type: CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT subscriptions_pkey PRIMARY KEY (id);


--
-- Name: uploaded_files uploaded_files_pkey; Type: CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.uploaded_files
    ADD CONSTRAINT uploaded_files_pkey PRIMARY KEY (file_key);


--
-- Name: orders uq_order_client_request; Type: CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT uq_order_client_request UNIQUE (customer_id, client_request_id);


--
-- Name: ratings uq_rating_order_rater; Type: CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.ratings
    ADD CONSTRAINT uq_rating_order_rater UNIQUE (order_id, rater_id);


--
-- Name: subscription_trips uq_sub_trip; Type: CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.subscription_trips
    ADD CONSTRAINT uq_sub_trip UNIQUE (subscription_id, trip_date, leg);


--
-- Name: user_devices uq_user_device; Type: CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.user_devices
    ADD CONSTRAINT uq_user_device UNIQUE (user_id, device_id);


--
-- Name: user_devices user_devices_pkey; Type: CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.user_devices
    ADD CONSTRAINT user_devices_pkey PRIMARY KEY (id);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: vehicle_documents vehicle_documents_pkey; Type: CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.vehicle_documents
    ADD CONSTRAINT vehicle_documents_pkey PRIMARY KEY (id);


--
-- Name: vehicles vehicles_driver_id_key; Type: CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.vehicles
    ADD CONSTRAINT vehicles_driver_id_key UNIQUE (driver_id);


--
-- Name: vehicles vehicles_pkey; Type: CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.vehicles
    ADD CONSTRAINT vehicles_pkey PRIMARY KEY (id);


--
-- Name: vehicles vehicles_plate_number_key; Type: CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.vehicles
    ADD CONSTRAINT vehicles_plate_number_key UNIQUE (plate_number);


--
-- Name: ix_admin_audit_logs_action; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_admin_audit_logs_action ON public.admin_audit_logs USING btree (action);


--
-- Name: ix_app_open_events_created_at; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_app_open_events_created_at ON public.app_open_events USING btree (created_at);


--
-- Name: ix_app_open_events_user_id; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_app_open_events_user_id ON public.app_open_events USING btree (user_id);


--
-- Name: ix_complaints_user_id; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_complaints_user_id ON public.complaints USING btree (user_id);


--
-- Name: ix_device_tokens_user_id; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_device_tokens_user_id ON public.device_tokens USING btree (user_id);


--
-- Name: ix_driver_payouts_driver_id; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_driver_payouts_driver_id ON public.driver_payouts USING btree (driver_id);


--
-- Name: ix_driver_payouts_status; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_driver_payouts_status ON public.driver_payouts USING btree (status);


--
-- Name: ix_driver_profiles_is_online; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_driver_profiles_is_online ON public.driver_profiles USING btree (is_online);


--
-- Name: ix_driver_work_sessions_driver_id; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_driver_work_sessions_driver_id ON public.driver_work_sessions USING btree (driver_id);


--
-- Name: ix_location_snapshots_created_at; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_location_snapshots_created_at ON public.location_snapshots USING btree (created_at);


--
-- Name: ix_location_snapshots_user_id; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_location_snapshots_user_id ON public.location_snapshots USING btree (user_id);


--
-- Name: ix_login_events_created_at; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_login_events_created_at ON public.login_events USING btree (created_at);


--
-- Name: ix_login_events_user_id; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_login_events_user_id ON public.login_events USING btree (user_id);


--
-- Name: ix_notifications_user_id; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_notifications_user_id ON public.notifications USING btree (user_id);


--
-- Name: ix_offer_driver_status; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_offer_driver_status ON public.order_offers USING btree (driver_id, offer_status);


--
-- Name: ix_order_offers_order_id; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_order_offers_order_id ON public.order_offers USING btree (order_id);


--
-- Name: ix_order_status_history_order_id; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_order_status_history_order_id ON public.order_status_history USING btree (order_id);


--
-- Name: ix_orders_customer_id; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_orders_customer_id ON public.orders USING btree (customer_id);


--
-- Name: ix_orders_driver_id; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_orders_driver_id ON public.orders USING btree (driver_id);


--
-- Name: ix_orders_status_created; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_orders_status_created ON public.orders USING btree (order_status, created_at);


--
-- Name: ix_payments_customer_id; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_payments_customer_id ON public.payments USING btree (customer_id);


--
-- Name: ix_payments_order_id; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_payments_order_id ON public.payments USING btree (order_id);


--
-- Name: ix_payments_payment_status; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_payments_payment_status ON public.payments USING btree (payment_status);


--
-- Name: ix_payments_subscription_id; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_payments_subscription_id ON public.payments USING btree (subscription_id);


--
-- Name: ix_ratings_order_id; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_ratings_order_id ON public.ratings USING btree (order_id);


--
-- Name: ix_ratings_ratee_id; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_ratings_ratee_id ON public.ratings USING btree (ratee_id);


--
-- Name: ix_ratings_rater_id; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_ratings_rater_id ON public.ratings USING btree (rater_id);


--
-- Name: ix_subscription_trips_driver_id; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_subscription_trips_driver_id ON public.subscription_trips USING btree (driver_id);


--
-- Name: ix_subscription_trips_subscription_id; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_subscription_trips_subscription_id ON public.subscription_trips USING btree (subscription_id);


--
-- Name: ix_subscription_trips_trip_date; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_subscription_trips_trip_date ON public.subscription_trips USING btree (trip_date);


--
-- Name: ix_subscriptions_customer_id; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_subscriptions_customer_id ON public.subscriptions USING btree (customer_id);


--
-- Name: ix_subscriptions_driver_id; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_subscriptions_driver_id ON public.subscriptions USING btree (driver_id);


--
-- Name: ix_subscriptions_status; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_subscriptions_status ON public.subscriptions USING btree (status);


--
-- Name: ix_uploaded_files_owner_id; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_uploaded_files_owner_id ON public.uploaded_files USING btree (owner_id);


--
-- Name: ix_user_devices_user_id; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_user_devices_user_id ON public.user_devices USING btree (user_id);


--
-- Name: ix_users_account_status; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_users_account_status ON public.users USING btree (account_status);


--
-- Name: ix_users_cpr_hash; Type: INDEX; Schema: public; Owner: transport
--

CREATE UNIQUE INDEX ix_users_cpr_hash ON public.users USING btree (cpr_hash);


--
-- Name: ix_users_last_seen_at; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_users_last_seen_at ON public.users USING btree (last_seen_at);


--
-- Name: ix_users_phone; Type: INDEX; Schema: public; Owner: transport
--

CREATE UNIQUE INDEX ix_users_phone ON public.users USING btree (phone);


--
-- Name: ix_users_role; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_users_role ON public.users USING btree (role);


--
-- Name: ix_vehicle_documents_vehicle_id; Type: INDEX; Schema: public; Owner: transport
--

CREATE INDEX ix_vehicle_documents_vehicle_id ON public.vehicle_documents USING btree (vehicle_id);


--
-- Name: admin_audit_logs admin_audit_logs_admin_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.admin_audit_logs
    ADD CONSTRAINT admin_audit_logs_admin_id_fkey FOREIGN KEY (admin_id) REFERENCES public.users(id);


--
-- Name: app_open_events app_open_events_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.app_open_events
    ADD CONSTRAINT app_open_events_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);


--
-- Name: app_settings app_settings_updated_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.app_settings
    ADD CONSTRAINT app_settings_updated_by_fkey FOREIGN KEY (updated_by) REFERENCES public.users(id);


--
-- Name: complaints complaints_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.complaints
    ADD CONSTRAINT complaints_order_id_fkey FOREIGN KEY (order_id) REFERENCES public.orders(id);


--
-- Name: complaints complaints_resolved_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.complaints
    ADD CONSTRAINT complaints_resolved_by_fkey FOREIGN KEY (resolved_by) REFERENCES public.users(id);


--
-- Name: complaints complaints_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.complaints
    ADD CONSTRAINT complaints_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);


--
-- Name: device_tokens device_tokens_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.device_tokens
    ADD CONSTRAINT device_tokens_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);


--
-- Name: driver_payouts driver_payouts_driver_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.driver_payouts
    ADD CONSTRAINT driver_payouts_driver_id_fkey FOREIGN KEY (driver_id) REFERENCES public.users(id);


--
-- Name: driver_payouts driver_payouts_payment_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.driver_payouts
    ADD CONSTRAINT driver_payouts_payment_id_fkey FOREIGN KEY (payment_id) REFERENCES public.payments(id);


--
-- Name: driver_profiles driver_profiles_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.driver_profiles
    ADD CONSTRAINT driver_profiles_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);


--
-- Name: driver_service_areas driver_service_areas_driver_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.driver_service_areas
    ADD CONSTRAINT driver_service_areas_driver_id_fkey FOREIGN KEY (driver_id) REFERENCES public.users(id);


--
-- Name: driver_service_areas driver_service_areas_service_area_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.driver_service_areas
    ADD CONSTRAINT driver_service_areas_service_area_id_fkey FOREIGN KEY (service_area_id) REFERENCES public.service_areas(id);


--
-- Name: driver_work_sessions driver_work_sessions_driver_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.driver_work_sessions
    ADD CONSTRAINT driver_work_sessions_driver_id_fkey FOREIGN KEY (driver_id) REFERENCES public.users(id);


--
-- Name: location_snapshots location_snapshots_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.location_snapshots
    ADD CONSTRAINT location_snapshots_order_id_fkey FOREIGN KEY (order_id) REFERENCES public.orders(id);


--
-- Name: location_snapshots location_snapshots_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.location_snapshots
    ADD CONSTRAINT location_snapshots_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);


--
-- Name: login_events login_events_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.login_events
    ADD CONSTRAINT login_events_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);


--
-- Name: notifications notifications_related_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT notifications_related_order_id_fkey FOREIGN KEY (related_order_id) REFERENCES public.orders(id);


--
-- Name: notifications notifications_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT notifications_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);


--
-- Name: order_offers order_offers_driver_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.order_offers
    ADD CONSTRAINT order_offers_driver_id_fkey FOREIGN KEY (driver_id) REFERENCES public.users(id);


--
-- Name: order_offers order_offers_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.order_offers
    ADD CONSTRAINT order_offers_order_id_fkey FOREIGN KEY (order_id) REFERENCES public.orders(id);


--
-- Name: order_status_history order_status_history_changed_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.order_status_history
    ADD CONSTRAINT order_status_history_changed_by_fkey FOREIGN KEY (changed_by) REFERENCES public.users(id);


--
-- Name: order_status_history order_status_history_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.order_status_history
    ADD CONSTRAINT order_status_history_order_id_fkey FOREIGN KEY (order_id) REFERENCES public.orders(id);


--
-- Name: orders orders_cancelled_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_cancelled_by_fkey FOREIGN KEY (cancelled_by) REFERENCES public.users(id);


--
-- Name: orders orders_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.users(id);


--
-- Name: orders orders_driver_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_driver_id_fkey FOREIGN KEY (driver_id) REFERENCES public.users(id);


--
-- Name: orders orders_service_area_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_service_area_id_fkey FOREIGN KEY (service_area_id) REFERENCES public.service_areas(id);


--
-- Name: payments payments_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.users(id);


--
-- Name: payments payments_driver_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_driver_id_fkey FOREIGN KEY (driver_id) REFERENCES public.users(id);


--
-- Name: payments payments_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_order_id_fkey FOREIGN KEY (order_id) REFERENCES public.orders(id);


--
-- Name: payments payments_subscription_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES public.subscriptions(id);


--
-- Name: payments payments_verified_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_verified_by_fkey FOREIGN KEY (verified_by) REFERENCES public.users(id);


--
-- Name: ratings ratings_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.ratings
    ADD CONSTRAINT ratings_order_id_fkey FOREIGN KEY (order_id) REFERENCES public.orders(id);


--
-- Name: ratings ratings_ratee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.ratings
    ADD CONSTRAINT ratings_ratee_id_fkey FOREIGN KEY (ratee_id) REFERENCES public.users(id);


--
-- Name: ratings ratings_rater_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.ratings
    ADD CONSTRAINT ratings_rater_id_fkey FOREIGN KEY (rater_id) REFERENCES public.users(id);


--
-- Name: subscription_trips subscription_trips_driver_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.subscription_trips
    ADD CONSTRAINT subscription_trips_driver_id_fkey FOREIGN KEY (driver_id) REFERENCES public.users(id);


--
-- Name: subscription_trips subscription_trips_subscription_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.subscription_trips
    ADD CONSTRAINT subscription_trips_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES public.subscriptions(id);


--
-- Name: subscriptions subscriptions_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT subscriptions_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.users(id);


--
-- Name: subscriptions subscriptions_driver_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT subscriptions_driver_id_fkey FOREIGN KEY (driver_id) REFERENCES public.users(id);


--
-- Name: subscriptions subscriptions_service_area_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT subscriptions_service_area_id_fkey FOREIGN KEY (service_area_id) REFERENCES public.service_areas(id);


--
-- Name: uploaded_files uploaded_files_owner_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.uploaded_files
    ADD CONSTRAINT uploaded_files_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.users(id);


--
-- Name: user_devices user_devices_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.user_devices
    ADD CONSTRAINT user_devices_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);


--
-- Name: users users_approved_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_approved_by_fkey FOREIGN KEY (approved_by) REFERENCES public.users(id);


--
-- Name: vehicle_documents vehicle_documents_reviewed_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.vehicle_documents
    ADD CONSTRAINT vehicle_documents_reviewed_by_fkey FOREIGN KEY (reviewed_by) REFERENCES public.users(id);


--
-- Name: vehicle_documents vehicle_documents_vehicle_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.vehicle_documents
    ADD CONSTRAINT vehicle_documents_vehicle_id_fkey FOREIGN KEY (vehicle_id) REFERENCES public.vehicles(id);


--
-- Name: vehicles vehicles_driver_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: transport
--

ALTER TABLE ONLY public.vehicles
    ADD CONSTRAINT vehicles_driver_id_fkey FOREIGN KEY (driver_id) REFERENCES public.users(id);


--
-- PostgreSQL database dump complete
--

\unrestrict mtINAyWqzwdsYmMdMqWNhsFfbVhpgm9b7tBD3LY3tL6FFFvl2gYhv8tUphGicFU

