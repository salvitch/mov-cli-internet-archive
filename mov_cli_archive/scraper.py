from __future__ import annotations
from typing import TYPE_CHECKING, Optional, Dict, Generator, Any

if TYPE_CHECKING:
    from mov_cli import Config
    from mov_cli.http_client import HTTPClient
    from mov_cli.scraper import ScraperOptionsT

from mov_cli import Single, Multi, Metadata
from mov_cli.scraper import Scraper
from mov_cli.utils import EpisodeSelector
import internetarchive

__all__ = ("ArchiveScraper", )

class ArchiveScraper(Scraper):
    def __init__(self, config: Config, http_client: HTTPClient, options: Optional[ScraperOptionsT] = None) -> None:
        super().__init__(config, http_client, options)
        
        self.username = self.options.get("username", None) if self.options else None
        self.password = self.options.get("password", None) if self.options else None
        self.default_limit = self.options.get("limit", 100) if self.options else 100
        
        if self.username and self.password:
            # Configure internetarchive with user credentials if provided
            internetarchive.configure(self.username, self.password)

    def search(self, query: str, limit: int | None = None) -> Generator[Metadata, Any, None]:
        actual_limit = limit if limit is not None else self.default_limit
        if actual_limit == 0 or actual_limit == -1:
            actual_limit = None  # Unlimited

        if not query:
            # Interactive Catalog Mode
            import inquirer
            
            # Curated popular collections
            choices = [
                ("Feature Films", "feature_films"),
                ("Anime (General)", "anime"),
                ("Anime Series", "anime-series"),
                ("Animation & Cartoons", "animationandcartoons"),
                ("Sci-Fi & Horror", "SciFi_Horror"),
                ("Comedy Films", "Comedy_Films"),
                ("Classic Television", "television"),
                ("TV Archive", "tvarchive"),
                ("Short Films", "short_films"),
                ("Silent Films", "silent_films"),
                ("Film Noir", "Film_Noir")
            ]
            
            questions = [
                inquirer.List('collection', message="Archive.org Catalog - Select a Collection to browse", choices=choices)
            ]
            answer = inquirer.prompt(questions)
            if not answer:
                return # User cancelled
                
            selected_collection = answer['collection']
            
            # Secondary prompt to search within the collection
            search_q = [
                inquirer.Text('term', message=f"Search within '{selected_collection}' (Leave empty to just list popular items)")
            ]
            term_ans = inquirer.prompt(search_q)
            term = term_ans['term'].strip() if term_ans else ""
            
            if term:
                search_query = f'({term}) AND mediatype:movies AND collection:{selected_collection}'
            else:
                search_query = f'mediatype:movies AND collection:{selected_collection}'
                
            results = internetarchive.search_items(search_query, sorts=['downloads desc'])
            
        elif query.startswith("collection:"):
            # Direct collection filter mode, e.g., "collection:anime-series haruhi"
            parts = query.split(" ", 1)
            col_part = parts[0].split("collection:")[1].strip()
            
            search_query = f'mediatype:movies AND collection:{col_part}'
            if len(parts) > 1:
                sub_query = parts[1].strip()
                search_query = f'({sub_query}) AND ' + search_query
                
            results = internetarchive.search_items(search_query, sorts=['downloads desc'])
            
        else:
            # Standard search
            search_query = f'({query}) AND mediatype:movies'
            results = internetarchive.search_items(search_query)
            
        count = 0
        for result in results:
            if actual_limit is not None and count >= actual_limit:
                break
            
            identifier = result['identifier']
            title = result.get('title', identifier)
            year = result.get('year', None)
            
            yield Metadata(
                id=identifier,
                title=title,
                type=1, # 1 for Movie
                year=year
            )
            count += 1

    def scrape_episodes(self, metadata: Metadata) -> Dict[int, int] | Dict[None, int]:
        return {None: 1}
    
    def scrape(self, metadata: Metadata, episode: EpisodeSelector) -> Single | Multi:
        item = internetarchive.get_item(metadata.id)
        
        video_url = None
        for f in item.get_files():
            # Look for common video formats
            if f.format in ['h.264', 'MPEG4', 'Matroska', 'Ogg Video']:
                video_url = f.url
                break
                
        if not video_url:
            raise Exception("No playable video file found in this archive item.")
            
        return Single(
            url=video_url,
            title=metadata.title,
            year=metadata.year
        )
