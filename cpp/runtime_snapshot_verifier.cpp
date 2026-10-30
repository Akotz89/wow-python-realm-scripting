// Standalone proof of the fail-closed C++ boundary for population snapshots.
// This is not yet an AzerothCore module or live-runtime integration.

#include <boost/json.hpp>
#include <openssl/evp.h>

#include <algorithm>
#include <array>
#include <cmath>
#include <cctype>
#include <cstdint>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <memory>
#include <regex>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace json = boost::json;

namespace
{
using Object = json::object;
using Array = json::array;

std::string ReadFile(std::string const& path)
{
    std::ifstream input(path, std::ios::binary);
    if (!input)
        throw std::runtime_error("cannot open snapshot: " + path);
    std::ostringstream content;
    content << input.rdbuf();
    if (!input.good() && !input.eof())
        throw std::runtime_error("cannot read snapshot: " + path);
    return content.str();
}

void RequireExactKeys(Object const& object, std::set<std::string> const& expected, std::string_view name)
{
    std::set<std::string> actual;
    for (auto const& item : object)
        actual.emplace(item.key());
    if (actual != expected)
        throw std::runtime_error(std::string(name) + " has missing or unknown fields");
}

Object const& RequireObject(Object const& object, std::string_view key)
{
    auto const& value = object.at(key);
    if (!value.is_object())
        throw std::runtime_error(std::string(key) + " must be an object");
    return value.as_object();
}

Array const& RequireArray(Object const& object, std::string_view key)
{
    auto const& value = object.at(key);
    if (!value.is_array())
        throw std::runtime_error(std::string(key) + " must be an array");
    return value.as_array();
}

std::string RequireString(Object const& object, std::string_view key)
{
    auto const& value = object.at(key);
    if (!value.is_string())
        throw std::runtime_error(std::string(key) + " must be a string");
    return std::string(value.as_string());
}

std::uint64_t RequireUInt(Object const& object, std::string_view key, std::uint64_t minimum = 0)
{
    auto const& value = object.at(key);
    std::uint64_t result = 0;
    if (value.is_uint64())
        result = value.as_uint64();
    else if (value.is_int64() && value.as_int64() >= 0)
        result = static_cast<std::uint64_t>(value.as_int64());
    else
        throw std::runtime_error(std::string(key) + " must be a non-negative integer");
    if (result < minimum)
        throw std::runtime_error(std::string(key) + " is below its minimum");
    return result;
}

bool RequireBool(Object const& object, std::string_view key)
{
    auto const& value = object.at(key);
    if (!value.is_bool())
        throw std::runtime_error(std::string(key) + " must be a boolean");
    return value.as_bool();
}

void RequireToken(std::string const& value, std::string_view name)
{
    static std::regex const token("^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$");
    if (!std::regex_match(value, token))
        throw std::runtime_error(std::string(name) + " is not a stable token");
}

void RequireDigest(std::string const& value, std::string_view name)
{
    if (value.size() != 64 || !std::ranges::all_of(value, [](unsigned char ch)
        {
            return std::isdigit(ch) || (ch >= 'a' && ch <= 'f');
        }))
        throw std::runtime_error(std::string(name) + " is not lowercase SHA-256");
}

std::string CanonicalJson(json::value const& value)
{
    if (value.is_object())
    {
        auto const& object = value.as_object();
        std::vector<std::string> keys;
        keys.reserve(object.size());
        for (auto const& item : object)
            keys.emplace_back(item.key());
        std::ranges::sort(keys);
        std::string result = "{";
        for (std::size_t index = 0; index < keys.size(); ++index)
        {
            if (index)
                result += ',';
            result += json::serialize(json::string(keys[index]));
            result += ':';
            result += CanonicalJson(object.at(keys[index]));
        }
        result += '}';
        return result;
    }
    if (value.is_array())
    {
        auto const& array = value.as_array();
        std::string result = "[";
        for (std::size_t index = 0; index < array.size(); ++index)
        {
            if (index)
                result += ',';
            result += CanonicalJson(array[index]);
        }
        result += ']';
        return result;
    }
    if (value.is_double())
        throw std::runtime_error("floating-point numbers are forbidden in a runtime snapshot");
    return json::serialize(value);
}

std::string Sha256(std::string_view content)
{
    using Context = std::unique_ptr<EVP_MD_CTX, decltype(&EVP_MD_CTX_free)>;
    Context context(EVP_MD_CTX_new(), EVP_MD_CTX_free);
    if (!context || EVP_DigestInit_ex(context.get(), EVP_sha256(), nullptr) != 1 ||
        EVP_DigestUpdate(context.get(), content.data(), content.size()) != 1)
        throw std::runtime_error("OpenSSL SHA-256 initialization failed");
    std::array<unsigned char, EVP_MAX_MD_SIZE> digest{};
    unsigned int length = 0;
    if (EVP_DigestFinal_ex(context.get(), digest.data(), &length) != 1 || length != 32)
        throw std::runtime_error("OpenSSL SHA-256 finalization failed");
    std::ostringstream text;
    text << std::hex << std::setfill('0');
    for (unsigned int index = 0; index < length; ++index)
        text << std::setw(2) << static_cast<unsigned int>(digest[index]);
    return text.str();
}

std::vector<std::string> RequireStringArray(Array const& array, std::string_view name)
{
    std::vector<std::string> result;
    result.reserve(array.size());
    for (auto const& value : array)
    {
        if (!value.is_string())
            throw std::runtime_error(std::string(name) + " must contain only strings");
        result.emplace_back(value.as_string());
    }
    if (!std::ranges::is_sorted(result) || std::ranges::adjacent_find(result) != result.end())
        throw std::runtime_error(std::string(name) + " must be sorted and unique");
    return result;
}

void VerifyBands(Object const& root, std::uint64_t phaseCap, std::uint64_t maximumOwnerLevel,
    std::uint64_t permanentTarget)
{
    auto const& bands = RequireArray(root, "bands");
    auto const& epochs = RequireArray(root, "band_capacity_epochs");
    if (bands.empty() || epochs.size() != maximumOwnerLevel)
        throw std::runtime_error("band and epoch cardinality is invalid");

    std::vector<std::string> bandNames;
    std::uint64_t priorCeiling = phaseCap;
    long double shareSum = 0;
    static std::regex const sharePattern("^(0|1|0\\.[0-9]+)$");
    for (auto const& value : bands)
    {
        if (!value.is_object())
            throw std::runtime_error("bands must contain objects");
        auto const& band = value.as_object();
        RequireExactKeys(band, {"ceiling", "name", "share"}, "band");
        auto name = RequireString(band, "name");
        RequireToken(name, "band.name");
        if (std::ranges::find(bandNames, name) != bandNames.end())
            throw std::runtime_error("band names must be unique");
        bandNames.push_back(name);
        auto const ceiling = RequireUInt(band, "ceiling", 1);
        if (ceiling > phaseCap || ceiling > priorCeiling)
            throw std::runtime_error("band ceilings must be non-increasing and phase bounded");
        priorCeiling = ceiling;
        auto const share = RequireString(band, "share");
        if (!std::regex_match(share, sharePattern))
            throw std::runtime_error("band share is not canonical");
        shareSum += std::stold(share);
    }
    if (std::abs(shareSum - 1.0L) > 1e-15L)
        throw std::runtime_error("band shares do not sum to one");

    std::uint64_t priorPopulation = 0;
    for (std::size_t index = 0; index < epochs.size(); ++index)
    {
        auto const& value = epochs[index];
        if (!value.is_object())
            throw std::runtime_error("band_capacity_epochs must contain objects");
        auto const& epoch = value.as_object();
        RequireExactKeys(
            epoch,
            {"capacities", "owner_level", "scheduled_cumulative_population"},
            "band capacity epoch");
        if (RequireUInt(epoch, "owner_level", 1) != index + 1)
            throw std::runtime_error("epoch owner levels must be contiguous");
        auto const population = RequireUInt(epoch, "scheduled_cumulative_population");
        if (population < priorPopulation)
            throw std::runtime_error("scheduled cumulative population must not decrease");
        priorPopulation = population;
        auto const& capacities = RequireObject(epoch, "capacities");
        std::set<std::string> expectedNames(bandNames.begin(), bandNames.end());
        RequireExactKeys(capacities, expectedNames, "epoch capacities");
        std::uint64_t capacitySum = 0;
        for (auto const& name : bandNames)
            capacitySum += RequireUInt(capacities, name);
        if (capacitySum != population)
            throw std::runtime_error("epoch capacities do not sum to scheduled population");
    }
    if (priorPopulation != permanentTarget)
        throw std::runtime_error("final epoch population does not equal permanent roster target");
}

void VerifyCapacity(Object const& root)
{
    auto const& capacity = RequireObject(root, "capacity_limits");
    RequireExactKeys(
        capacity,
        {
            "concurrent_active_target",
            "creation_batch_size",
            "login_batch_size",
            "max_active_per_map",
            "max_active_per_starting_region",
            "max_active_per_zone",
            "safety_reserve",
            "server_session_budget",
        },
        "capacity_limits");
    auto const sessionBudget = RequireUInt(capacity, "server_session_budget", 1);
    auto const safetyReserve = RequireUInt(capacity, "safety_reserve", 5);
    auto const activeTarget = RequireUInt(capacity, "concurrent_active_target", 1);
    auto const creationBatch = RequireUInt(capacity, "creation_batch_size", 1);
    auto const loginBatch = RequireUInt(capacity, "login_batch_size", 1);
    auto const map = RequireUInt(capacity, "max_active_per_map", 1);
    auto const zone = RequireUInt(capacity, "max_active_per_zone", 1);
    auto const startingRegion = RequireUInt(capacity, "max_active_per_starting_region", 1);
    if (activeTarget > sessionBudget || safetyReserve > sessionBudget - activeTarget)
        throw std::runtime_error("active target plus safety reserve exceeds session budget");
    if (creationBatch > activeTarget || loginBatch > activeTarget)
        throw std::runtime_error("creation or login batch exceeds active target");
    if (startingRegion > zone || zone > map || map > activeTarget)
        throw std::runtime_error("density limits must be ordered starting-region <= zone <= map <= global");
}

void VerifyHold(Object const& root)
{
    auto const& hold = RequireObject(root, "progression_hold");
    RequireExactKeys(
        hold,
        {
            "allowed_activities",
            "bank_xp",
            "delevel_on_demotion",
            "denied_progression_mutations",
            "unknown_activity",
        },
        "progression_hold");
    if (RequireBool(hold, "bank_xp") || RequireBool(hold, "delevel_on_demotion"))
        throw std::runtime_error("held bots may not bank XP or be deleveled");
    if (RequireString(hold, "unknown_activity") != "deny")
        throw std::runtime_error("unknown held-bot activities must be denied");

    std::set<std::string> const allowedTypes = {
        "combat_farming",
        "crafting",
        "eligible_dungeon",
        "fishing",
        "gathering",
        "gear_improvement",
        "mail",
        "phase_safe_travel",
        "profession_training",
        "repair",
        "social",
        "trade",
    };
    auto const allowed = RequireStringArray(RequireArray(hold, "allowed_activities"), "allowed_activities");
    if (!std::ranges::all_of(allowed, [&](auto const& item) { return allowedTypes.contains(item); }))
        throw std::runtime_error("held activity whitelist contains an unknown activity");

    std::vector<std::string> const denied = {
        "banked_xp",
        "quest_accept",
        "quest_complete",
        "quest_objective_credit",
        "quest_reward",
        "quest_script_complete",
        "quest_turn_in",
        "xp_award",
    };
    if (RequireStringArray(
            RequireArray(hold, "denied_progression_mutations"),
            "denied_progression_mutations") != denied)
        throw std::runtime_error("progression hold denial set is not exact");
}

void VerifyFailurePolicy(Object const& root)
{
    auto const& policy = RequireObject(root, "failure_policy");
    RequireExactKeys(
        policy,
        {"invalid_snapshot", "missing_snapshot", "runtime_reload_failure"},
        "failure_policy");
    if (RequireString(policy, "invalid_snapshot") != "reject" ||
        RequireString(policy, "missing_snapshot") != "fail_closed" ||
        RequireString(policy, "runtime_reload_failure") != "retain_last_verified_or_fail_closed")
        throw std::runtime_error("runtime failure policy is not fail closed");
}

void VerifySnapshot(Object root)
{
    RequireExactKeys(
        root,
        {
            "band_capacity_epochs",
            "bands",
            "binding_count",
            "capacity_limits",
            "effective_ceiling_rule",
            "failure_policy",
            "global_phase_cap",
            "manifest_identity_count",
            "maximum_owner_level",
            "owner_anchor_guid",
            "permanent_roster_target",
            "progression_hold",
            "realm_id",
            "snapshot_digest",
            "snapshot_schema",
            "snapshot_version",
            "source_digests",
        },
        "runtime snapshot");

    if (RequireUInt(root, "snapshot_schema", 1) != 1)
        throw std::runtime_error("unsupported snapshot_schema");
    RequireToken(RequireString(root, "snapshot_version"), "snapshot_version");
    RequireToken(RequireString(root, "realm_id"), "realm_id");
    auto const expectedDigest = RequireString(root, "snapshot_digest");
    RequireDigest(expectedDigest, "snapshot_digest");
    root.erase("snapshot_digest");
    if (Sha256(CanonicalJson(root)) != expectedDigest)
        throw std::runtime_error("snapshot_digest does not match canonical payload");

    auto const& sources = RequireObject(root, "source_digests");
    RequireExactKeys(
        sources,
        {"binding_digest", "manifest_digest", "policy_digest", "schedule_digest"},
        "source_digests");
    for (auto const& name : {"binding_digest", "manifest_digest", "policy_digest", "schedule_digest"})
        RequireDigest(RequireString(sources, name), name);

    auto const maximumOwnerLevel = RequireUInt(root, "maximum_owner_level", 1);
    auto const permanentTarget = RequireUInt(root, "permanent_roster_target", 1);
    auto const identityCount = RequireUInt(root, "manifest_identity_count", 1);
    auto const bindingCount = RequireUInt(root, "binding_count");
    auto const phaseCap = RequireUInt(root, "global_phase_cap", 1);
    RequireUInt(root, "owner_anchor_guid", 1);
    if (maximumOwnerLevel > 80 || phaseCap > maximumOwnerLevel || identityCount != permanentTarget ||
        bindingCount > identityCount)
        throw std::runtime_error("owner level phase or identity cardinality invariant failed");
    if (RequireString(root, "effective_ceiling_rule") !=
        "min(global_phase_cap,dynamic_band_ceiling)")
        throw std::runtime_error("effective ceiling rule is unsupported");

    VerifyBands(root, phaseCap, maximumOwnerLevel, permanentTarget);
    VerifyCapacity(root);
    VerifyHold(root);
    VerifyFailurePolicy(root);
}
} // namespace

int main(int argc, char** argv)
{
    if (argc != 2)
    {
        std::cerr << "usage: runtime_snapshot_verifier <snapshot.json>\n";
        return 2;
    }
    try
    {
        auto parsed = json::parse(ReadFile(argv[1]));
        if (!parsed.is_object())
            throw std::runtime_error("runtime snapshot must be a JSON object");
        VerifySnapshot(parsed.as_object());
        std::cout << "PASS: C++ accepted the canonical fail-closed population runtime snapshot\n";
        return 0;
    }
    catch (std::exception const& error)
    {
        std::cerr << "FAIL: " << error.what() << '\n';
        return 1;
    }
}
