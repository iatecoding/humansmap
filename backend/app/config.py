from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ArangoDB
    arango_host: str = "localhost"
    arango_port: int = 8529
    arango_db: str = "world_graph_all"
    arango_password: str
    arango_username: str = "root"

    # Redis (for rate limiting)
    redis_url: str = "redis://localhost:6379/0"

    # CORS
    allowed_origins: str = "http://localhost:3000"

    # Admin
    admin_token: str = ""

    # Dump processing
    dump_file: str = "/data/wikidump/latest-truthy.nt.bz2"
    dump_batch_size: int = 500
    pass1_output_dir: str = "/data/wikidump"

    # Data thresholds
    notability_min_statements: int = 1
    default_traversal_depth: int = 2
    max_traversal_depth: int = 3
    neighborhood_node_limit: int = 1000
    persons_page_size: int = 50

    # Entity enrichment
    rich_fields_min_statements: int = 50
    org_top_n_per_country: int = 100
    org_no_country_cap: int = 500

    # Game mode
    game_challenge_min_statements: int = 150
    game_challenge_min_degree: int = 3
    game_challenge_min_path: int = 2
    game_challenge_max_path: int = 6
    game_challenge_deck_size: int = 300

    # Image optimization
    image_max_size_bytes: int = 50_000
    image_max_dimension_px: int = 300
    image_jpeg_quality: int = 80
    image_cache_dir: str = "/app/images"
    image_min_statements: int = 50  # Only cache images for persons with >N Wikidata statements

    # Wikidata SPARQL
    sparql_endpoint: str = "https://query.wikidata.org/sparql"
    sparql_delay_seconds: float = 2.0
    sparql_user_agent: str = "HumansMap/1.0 (admin@humansmap.com) python-urllib/3.x"

    # SEC EDGAR
    sec_user_agent: str = "HumansMap admin@humansmap.com"
    sec_data_dir: str = "/data/sec"
    sec_requests_per_second: float = 8.0
    sec_form4_start_year: int = 2010

    # Redis
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_default_ttl: int = 86400    # 24h
    redis_path_ttl: int = 172800      # 48h

    # Trending / featured
    trending_min_statements: int = 100
    trending_cache_ttl: int = 86400   # 24h
    trending_refresh_count: int = 50  # how many to cache per language

    # Sync
    sync_interval_days: int = 7

    # Reporting (all optional — reports are always logged; email is sent only when host is set)
    report_email_to: str = ""        # destination address, e.g. you@example.com
    report_smtp_host: str = ""       # e.g. smtp.gmail.com
    report_smtp_port: int = 587
    report_smtp_tls: bool = True
    report_smtp_user: str = ""
    report_smtp_password: str = ""
    report_smtp_from: str = ""       # "From" address; defaults to report_email_to if blank

    model_config = {"env_prefix": "", "case_sensitive": False}


settings = Settings()
