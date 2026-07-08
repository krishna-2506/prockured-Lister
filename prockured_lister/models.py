from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime

@dataclass
class ProductData:
    basics: dict = field(default_factory=dict)
    attributes: list = field(default_factory=list)
    variant_attributes: list = field(default_factory=list)
    variant_pricing: list = field(default_factory=list)
    seo: dict = field(default_factory=dict)
    media: dict = field(default_factory=dict)
    pricing: dict = field(default_factory=dict)

BASICS_KEYS = [
    "Product Type", "Brand", "Product Name", "Slug",
    "Product Tags", "Description", "Short Description",
]
SEO_KEYS = ["SEO Title", "SEO Description", "SEO Keywords"]
MEDIA_KEYS = [
    "Main Image", "Image 1", "Image 2", "Image 3", "Image URL", "Image URLs",
    "Image Folder", "Folder", "Replace Existing Images", "Replace Images",
    "Alt Text Suffix", "Alt Text Prefix"
]
PRICING_KEYS = [
    "Price", "Sale Price", "Actual Price", "Discount Price", 
    "Base Price", "MRP", "Compare Price", "Cost Price", "GST"
]

class BatchLogger:
    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(f"\n--- Log started at {datetime.now()} ---\n")
            
    def write(self, msg=""):
        try:
            from .logger import logger
            if msg:
                logger.info(msg)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(str(msg) + "\n")
        except Exception as e:
            # Fallback if logging fails
            print(f"Logger error: {e}")
