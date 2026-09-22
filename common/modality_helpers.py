"""
Modality Helper Functions

This module provides helper functions for working with modalities dynamically,
without hardcoding modality slugs throughout the codebase.

All modality information should be retrieved from the database.
"""
from django.core.cache import cache
from common.models import Modality
from typing import Optional, List, Set
import logging

logger = logging.getLogger(__name__)

# Cache timeout in seconds (5 minutes)
MODALITY_CACHE_TIMEOUT = 60*5


def get_all_modalities() -> List[Modality]:
    """
    Get all active modalities from the database with caching.
    
    Returns:
        List of Modality objects
    """
    cache_key = 'all_modalities'
    modalities = cache.get(cache_key)
    
    if modalities is None:
        modalities = list(Modality.objects.filter(is_active=True).order_by('name'))
        cache.set(cache_key, modalities, MODALITY_CACHE_TIMEOUT)
    
    return modalities


def get_modality_by_slug(slug: str) -> Optional[Modality]:
    """
    Get a modality by its slug with caching.
    
    Args:
        slug: The modality slug
        
    Returns:
        Modality object or None if not found
    """
    if not slug:
        return None
        
    cache_key = f'modality_{slug}'
    modality = cache.get(cache_key)
    
    if modality is None:
        try:
            modality = Modality.objects.get(slug=slug, is_active=True)
            cache.set(cache_key, modality, MODALITY_CACHE_TIMEOUT)
        except Modality.DoesNotExist:
            logger.warning(f"Modality with slug '{slug}' not found")
            return None
    
    return modality


def get_modality_slugs() -> Set[str]:
    """
    Get all active modality slugs from the database with caching.
    
    Returns:
        Set of modality slug strings
    """
    cache_key = 'modality_slugs'
    slugs = cache.get(cache_key)
    
    if slugs is None:
        slugs = set(Modality.objects.filter(is_active=True).values_list('slug', flat=True))
        cache.set(cache_key, slugs, MODALITY_CACHE_TIMEOUT)
    
    return slugs


def is_valid_modality_slug(slug: str) -> bool:
    """
    Check if a slug corresponds to an active modality.
    
    Args:
        slug: The slug to check
        
    Returns:
        True if the slug is a valid active modality, False otherwise
    """
    return slug in get_modality_slugs()


def infer_modality_from_field_name(field_name: str) -> Optional[str]:
    """
    Infer modality slug from a file upload field name.
    
    Args:
        field_name: The name of the file input field
        
    Returns:
        Inferred modality slug or None if cannot be inferred
    """
    if not field_name:
        return None
    
    # Normalize folder variants
    inferred = field_name.replace('_folder_files', '').replace('-folder_files', '').strip()
    
    # Check if it's a valid modality
    if is_valid_modality_slug(inferred):
        return inferred
    
    return None


def get_modalities_for_uploaded_files(files_dict, allowed_slugs: Optional[Set[str]] = None) -> List[Modality]:
    """
    Determine which modalities were uploaded based on file field names.
    
    Args:
        files_dict: Django request.FILES dictionary
        allowed_slugs: Optional set of allowed modality slugs (for project restrictions)
        
    Returns:
        List of Modality objects that were uploaded
    """
    detected_slugs = set()
    
    # Check for IOS (upper + lower scans)
    if 'upper_scan' in files_dict and 'lower_scan' in files_dict:
        ios_modality = get_modality_by_slug('ios')
        if ios_modality and (not allowed_slugs or 'ios' in allowed_slugs):
            detected_slugs.add('ios')
    
    # Infer from other field names
    for field_name in files_dict.keys():
        inferred_slug = infer_modality_from_field_name(field_name)
        if inferred_slug and (not allowed_slugs or inferred_slug in allowed_slugs):
            detected_slugs.add(inferred_slug)
    
    # Convert slugs to Modality objects
    modalities = []
    for slug in detected_slugs:
        modality = get_modality_by_slug(slug)
        if modality:
            modalities.append(modality)
    
    return modalities


def clear_modality_cache():
    """
    Clear all modality-related cache entries.
    Useful when modalities are added/updated/deleted.
    """
    cache.delete('all_modalities')
    cache.delete('modality_slugs')
    
    # Also clear individual modality caches
    all_slugs = Modality.objects.values_list('slug', flat=True)
    for slug in all_slugs:
        cache.delete(f'modality_{slug}')

