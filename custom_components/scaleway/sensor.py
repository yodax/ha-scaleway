"""Sensor platform for the Scaleway integration.

Cost categories, Instances, Kubernetes clusters, and buckets are all
discovered dynamically from API responses rather than declared as a fixed
set — a category or resource only becomes a sensor once it's actually seen
in the data. Each coordinator refresh checks for not-yet-seen keys and adds
entities for them; already-added entities go `unavailable` (rather than
being removed) if their resource drops out of a later refresh, since
DataUpdateCoordinator has no built-in entity-removal hook.
"""
from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfInformation
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import DOMAIN
from .coordinator import ScalewayBucketsCoordinator, ScalewayCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Scaleway sensors for one Organization (config entry)."""
    coordinators = hass.data[DOMAIN][entry.entry_id]
    coordinator: ScalewayCoordinator = coordinators["main"]
    buckets_coordinator: ScalewayBucketsCoordinator = coordinators["buckets"]

    # Registered eagerly rather than left to the cost sensors' DeviceInfo: the
    # per-resource devices link back to it with `via_device_id`, which HA only
    # resolves against devices that are already in the registry.
    account_device_id = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="Scaleway",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url="https://console.scaleway.com",
    ).id
    account_device = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    known_categories: set[str] = set()
    known_instances: set[str] = set()
    known_clusters: set[str] = set()
    known_buckets: set[str] = set()

    @callback
    def _add_cost_entities() -> None:
        data = coordinator.data or {}
        new_categories = set(data.get("cost", {}).get("by_category", {})) - known_categories
        if not new_categories:
            return
        known_categories.update(new_categories)
        async_add_entities(
            ScalewayCostCategorySensor(coordinator, entry, account_device, category)
            for category in new_categories
        )

    @callback
    def _add_instance_entities() -> None:
        data = coordinator.data or {}
        instances_by_id = {i["id"]: i for i in data.get("instances", [])}
        new_ids = set(instances_by_id) - known_instances
        if not new_ids:
            return
        known_instances.update(new_ids)
        async_add_entities(
            ScalewayInstanceSensor(
                coordinator, entry, instances_by_id[instance_id], account_device_id
            )
            for instance_id in new_ids
        )

    @callback
    def _add_cluster_entities() -> None:
        data = coordinator.data or {}
        clusters_by_id = {c["id"]: c for c in data.get("clusters", [])}
        new_ids = set(clusters_by_id) - known_clusters
        if not new_ids:
            return
        known_clusters.update(new_ids)
        async_add_entities(
            ScalewayClusterSensor(
                coordinator, entry, clusters_by_id[cluster_id], account_device_id
            )
            for cluster_id in new_ids
        )

    @callback
    def _add_bucket_entities() -> None:
        data = buckets_coordinator.data or {}
        new_keys = set(data) - known_buckets
        if not new_keys:
            return
        known_buckets.update(new_keys)
        entities: list[SensorEntity] = []
        for key in new_keys:
            region, name = key.split(":", 1)
            entities.append(
                ScalewayBucketSizeSensor(
                    buckets_coordinator, entry, region, name, account_device_id
                )
            )
            entities.append(
                ScalewayBucketObjectCountSensor(
                    buckets_coordinator, entry, region, name, account_device_id
                )
            )
        async_add_entities(entities)

    async_add_entities([ScalewayCostTotalSensor(coordinator, entry, account_device)])
    _add_cost_entities()
    _add_instance_entities()
    _add_cluster_entities()
    coordinator.async_add_listener(_add_cost_entities)
    coordinator.async_add_listener(_add_instance_entities)
    coordinator.async_add_listener(_add_cluster_entities)

    if buckets_coordinator.buckets:
        _add_bucket_entities()
        buckets_coordinator.async_add_listener(_add_bucket_entities)


class ScalewayCostTotalSensor(CoordinatorEntity[ScalewayCoordinator], SensorEntity):
    """Total Scaleway spend for the current billing period."""

    _attr_has_entity_name = True
    _attr_translation_key = "cost_total"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator: ScalewayCoordinator, entry: ConfigEntry, device: DeviceInfo) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_cost_total"
        self._attr_device_info = device

    @property
    def native_value(self):
        if self.coordinator.data is None:
            return None
        return self.coordinator.data["cost"]["total"]

    @property
    def native_unit_of_measurement(self):
        if self.coordinator.data is None:
            return None
        return self.coordinator.data["cost"]["currency"]


class ScalewayCostCategorySensor(CoordinatorEntity[ScalewayCoordinator], SensorEntity):
    """Spend for one billing category (e.g. Object Storage, Instances).

    Categories are discovered from the API, not a fixed enum, so this
    entity sets its own name directly rather than using a translation_key.
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(
        self, coordinator: ScalewayCoordinator, entry: ConfigEntry, device: DeviceInfo, category: str
    ) -> None:
        super().__init__(coordinator)
        self._category = category
        self._attr_name = f"Cost - {category}"
        self._attr_unique_id = f"{entry.entry_id}_cost_{slugify(category)}"
        self._attr_device_info = device

    @property
    def native_value(self):
        if self.coordinator.data is None:
            return None
        return self.coordinator.data["cost"]["by_category"].get(self._category)

    @property
    def native_unit_of_measurement(self):
        if self.coordinator.data is None:
            return None
        return self.coordinator.data["cost"]["currency"]

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.data is not None
            and self._category in self.coordinator.data["cost"]["by_category"]
        )


class ScalewayInstanceSensor(CoordinatorEntity[ScalewayCoordinator], SensorEntity):
    """State of one Scaleway Instance (VPS)."""

    _attr_has_entity_name = True
    _attr_translation_key = "instance_state"

    def __init__(
        self,
        coordinator: ScalewayCoordinator,
        entry: ConfigEntry,
        instance: dict,
        via_device_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._instance_id = instance["id"]
        self._attr_unique_id = f"{entry.entry_id}_instance_{self._instance_id}_state"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._instance_id)},
            name=instance["name"],
            manufacturer="Scaleway",
            model=instance.get("commercial_type"),
            via_device_id=via_device_id,
        )

    @property
    def _instance(self) -> dict | None:
        if self.coordinator.data is None:
            return None
        return next(
            (i for i in self.coordinator.data["instances"] if i["id"] == self._instance_id), None
        )

    @property
    def native_value(self):
        instance = self._instance
        return instance["state"] if instance else None

    @property
    def extra_state_attributes(self):
        instance = self._instance
        if not instance:
            return {}
        return {"zone": instance["zone"], "commercial_type": instance.get("commercial_type")}

    @property
    def available(self) -> bool:
        return super().available and self._instance is not None


class ScalewayClusterSensor(CoordinatorEntity[ScalewayCoordinator], SensorEntity):
    """Status of one Kubernetes (Kapsule) cluster."""

    _attr_has_entity_name = True
    _attr_translation_key = "cluster_status"

    def __init__(
        self,
        coordinator: ScalewayCoordinator,
        entry: ConfigEntry,
        cluster: dict,
        via_device_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._cluster_id = cluster["id"]
        self._attr_unique_id = f"{entry.entry_id}_cluster_{self._cluster_id}_status"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._cluster_id)},
            name=cluster["name"],
            manufacturer="Scaleway",
            model="Kubernetes Kapsule",
            via_device_id=via_device_id,
        )

    @property
    def _cluster(self) -> dict | None:
        if self.coordinator.data is None:
            return None
        return next(
            (c for c in self.coordinator.data["clusters"] if c["id"] == self._cluster_id), None
        )

    @property
    def native_value(self):
        cluster = self._cluster
        return cluster["status"] if cluster else None

    @property
    def extra_state_attributes(self):
        cluster = self._cluster
        if not cluster:
            return {}
        return {"region": cluster["region"], "version": cluster.get("version")}

    @property
    def available(self) -> bool:
        return super().available and self._cluster is not None


class _ScalewayBucketSensorBase(CoordinatorEntity[ScalewayBucketsCoordinator], SensorEntity):
    """Shared device info for a monitored Object Storage bucket's sensors."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ScalewayBucketsCoordinator,
        entry: ConfigEntry,
        region: str,
        name: str,
        via_device_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._key = f"{region}:{name}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"bucket_{self._key}")},
            name=f"{name} ({region})",
            manufacturer="Scaleway",
            model="Object Storage bucket",
            via_device_id=via_device_id,
        )

    @property
    def _bucket(self) -> dict | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self._key)

    @property
    def available(self) -> bool:
        return super().available and self._bucket is not None


class ScalewayBucketSizeSensor(_ScalewayBucketSensorBase):
    """Total size of one monitored Object Storage bucket."""

    _attr_translation_key = "bucket_size"
    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_native_unit_of_measurement = UnitOfInformation.BYTES
    _attr_suggested_unit_of_measurement = UnitOfInformation.GIGABYTES
    _attr_suggested_display_precision = 2
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: ScalewayBucketsCoordinator,
        entry: ConfigEntry,
        region: str,
        name: str,
        via_device_id: str,
    ) -> None:
        super().__init__(coordinator, entry, region, name, via_device_id)
        self._attr_unique_id = f"{entry.entry_id}_bucket_{self._key}_size"

    @property
    def native_value(self):
        bucket = self._bucket
        return bucket["size_bytes"] if bucket else None


class ScalewayBucketObjectCountSensor(_ScalewayBucketSensorBase):
    """Object count of one monitored Object Storage bucket."""

    _attr_translation_key = "bucket_object_count"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: ScalewayBucketsCoordinator,
        entry: ConfigEntry,
        region: str,
        name: str,
        via_device_id: str,
    ) -> None:
        super().__init__(coordinator, entry, region, name, via_device_id)
        self._attr_unique_id = f"{entry.entry_id}_bucket_{self._key}_object_count"

    @property
    def native_value(self):
        bucket = self._bucket
        return bucket["object_count"] if bucket else None
