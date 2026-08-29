from __future__ import annotations

import pytest

from juno.homeassistant import Entity, HomeAssistant


@pytest.fixture()
def ha():
    return HomeAssistant("http://ha.local:8123", "token", forbidden=["lock.front_door", "cover.*"])


class TestGuards:
    def test_a_literal_entity_can_be_forbidden(self, ha):
        assert ha.is_forbidden("lock.front_door") is True
        assert ha.is_forbidden("lock.back_door") is False

    def test_a_domain_glob_forbids_the_whole_domain(self, ha):
        assert ha.is_forbidden("cover.garage") is True
        assert ha.is_forbidden("cover.blinds") is True

    def test_ordinary_entities_are_allowed(self, ha):
        assert ha.is_forbidden("light.kitchen") is False

    @pytest.mark.parametrize(
        "entity_id", ["lock.back_door", "cover.blinds", "alarm_control_panel.home", "vacuum.robot"]
    )
    def test_risky_domains_are_guarded_even_when_not_forbidden(self, ha, entity_id):
        """Guarded means 'needs explicit confirmation', not 'blocked' — a
        misheard sentence must not be able to unlock a door."""
        assert ha.is_guarded(entity_id) is True

    @pytest.mark.parametrize("entity_id", ["light.kitchen", "switch.lamp", "climate.hall"])
    def test_ordinary_domains_are_not_guarded(self, ha, entity_id):
        assert ha.is_guarded(entity_id) is False

    async def test_calling_a_forbidden_entity_is_refused_before_any_request(self, ha):
        """No HTTP call is made, so this passes with nothing listening."""
        assert "forbidden" in await ha.call("lock", "unlock", "lock.front_door")


class TestConfigured:
    def test_needs_both_url_and_token(self):
        assert HomeAssistant("http://ha", "").configured is False
        assert HomeAssistant("", "token").configured is False
        assert HomeAssistant("http://ha", "token").configured is True

    def test_a_trailing_slash_does_not_double_up(self):
        assert HomeAssistant("http://ha.local:8123/", "t").url == "http://ha.local:8123"


class TestEntity:
    def test_domain_is_taken_from_the_entity_id(self):
        assert Entity("light.kitchen", "Kitchen", "on").domain == "light"

    def test_describe_includes_id_name_and_state(self):
        described = Entity("light.kitchen", "Kitchen Ceiling", "on").describe()
        assert "light.kitchen" in described
        assert "Kitchen Ceiling" in described
        assert "on" in described
