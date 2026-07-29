"""Canonical market-data adapters and reproducibility evidence."""

from ea.data.fingerprint import (
    MarketDataSelection,
    select_and_fingerprint_market_data,
)
from ea.data.historical import (
    PHASE1_OHLCV_MAX_BYTES,
    PHASE1_OHLCV_MAX_FIELD_LENGTH,
    PHASE1_OHLCV_MAX_RECORDS,
    PHASE1_OHLCV_PROFILE,
    HistoricalMarketDataError,
    HistoricalMarketDataFailureCode,
    Phase1HistoricalDataset,
    Phase1HistoricalMarketDataSource,
    Phase1HistoricalSourceCursor,
    create_phase1_historical_market_data_source,
    decode_phase1_ohlcv_csv,
    read_phase1_ohlcv_csv,
)

__all__ = [
    "MarketDataSelection",
    "PHASE1_OHLCV_MAX_BYTES",
    "PHASE1_OHLCV_MAX_FIELD_LENGTH",
    "PHASE1_OHLCV_MAX_RECORDS",
    "PHASE1_OHLCV_PROFILE",
    "HistoricalMarketDataError",
    "HistoricalMarketDataFailureCode",
    "Phase1HistoricalDataset",
    "Phase1HistoricalMarketDataSource",
    "Phase1HistoricalSourceCursor",
    "create_phase1_historical_market_data_source",
    "decode_phase1_ohlcv_csv",
    "read_phase1_ohlcv_csv",
    "select_and_fingerprint_market_data",
]
