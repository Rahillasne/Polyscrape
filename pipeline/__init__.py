"""FundingDeadlines data pipeline.

A robust scraper plus a strict data-quality gate that keeps the committed
``src/data/deadlines.json`` dataset clean: junk is rejected and a broken
scrape can never blank out a good entry.
"""
