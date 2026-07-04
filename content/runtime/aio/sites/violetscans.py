import urllib3
from .mangathemesia import MangaThemesiaSiteHandler

class VioletScansSiteHandler(MangaThemesiaSiteHandler):
    name = "violetscans"
    display_name = "VioletScans"
    base_url = "https://violetscans.org"
    domains = ("violetscans.org", "www.violetscans.org")
    
    def __init__(self, *args, **kwargs):
        # Initialize with specific settings for VioletScans
        super().__init__(
            name=self.name,
            display_name=self.display_name,
            base_url=self.base_url,
            domains=self.domains,
            use_playwright=True,
            verify_ssl=True,
            *args, **kwargs
        )

