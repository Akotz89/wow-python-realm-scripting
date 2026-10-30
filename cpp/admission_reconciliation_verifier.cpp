// Standalone proof of the fail-closed C++ boundary for admission plans.
// This is not yet an AzerothCore module or live-runtime integration.

#include <boost/json.hpp>
#include <openssl/evp.h>

#include <algorithm>
#include <array>
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
        throw std::runtime_error("cannot open reconciliation plan: " + path);
    std::ostringstream content;
    content << input.rdbuf();
    if (!input.good() && !input.eof())
        throw std::runtime_error("cannot read reconciliation plan: " + path);
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

std::uint64_t RequireUInt(Object const& object, std::string_view key, std::uint64_t minimum = 0,
    std::uint64_t maximum = UINT64_MAX)
{
    auto const& value = object.at(key);
    std::uint64_t result = 0;
    if (value.is_uint64())
        result = value.as_uint64();
    else if (value.is_int64() && value.as_int64() >= 0)
        result = static_cast<std::uint64_t>(value.as_int64());
    else
        throw std::runtime_error(std::string(key) + " must be a non-negative integer");
    if (result < minimum || result > maximum)
        throw std::runtime_error(std::string(key) + " is outside its allowed range");
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
        throw std::runtime_error("floating-point numbers are forbidden in an admission plan");
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

std::vector<std::string> RequireUniqueTokens(Array const& array, std::string_view name)
{
    std::vector<std::string> result;
    std::set<std::string> unique;
    result.reserve(array.size());
    for (auto const& value : array)
    {
        if (!value.is_string())
            throw std::runtime_error(std::string(name) + " must contain only strings");
        auto token = std::string(value.as_string());
        RequireToken(token, name);
        if (!unique.emplace(token).second)
            throw std::runtime_error(std::string(name) + " must be unique");
        result.push_back(std::move(token));
    }
    return result;
}

std::vector<std::uint64_t> RequireIncreasingLevels(Array const& array, std::string_view name)
{
    std::vector<std::uint64_t> result;
    result.reserve(array.size());
    std::uint64_t prior = 0;
    for (auto const& value : array)
    {
        Object wrapper{{"level", value}};
        auto const level = RequireUInt(wrapper, "level", 1, 80);
        if (level <= prior)
            throw std::runtime_error(std::string(name) + " must be strictly increasing");
        result.push_back(level);
        prior = level;
    }
    return result;
}

struct WaveSummary
{
    std::uint64_t ownerLevel;
    std::uint64_t remainingCount;
    std::vector<std::string> ineligible;
};

std::vector<WaveSummary> VerifyWaveStates(Object const& root, std::uint64_t resultingHighWater,
    std::string const& realmId, std::uint64_t ownerAnchorGuid, std::string const& scheduleDigest)
{
    auto const& values = RequireArray(root, "wave_states");
    if (values.size() != resultingHighWater)
        throw std::runtime_error("wave_states must cover every authorized owner level");

    std::vector<WaveSummary> result;
    result.reserve(values.size());
    for (std::size_t index = 0; index < values.size(); ++index)
    {
        if (!values[index].is_object())
            throw std::runtime_error("wave_states must contain objects");
        auto const& wave = values[index].as_object();
        RequireExactKeys(
            wave,
            {"admitted_count", "idempotency_key", "ineligible_remaining", "owner_level",
                "planned_count", "remaining_count", "state"},
            "wave state");
        auto const ownerLevel = RequireUInt(wave, "owner_level", 1, 80);
        if (ownerLevel != index + 1)
            throw std::runtime_error("wave owner levels must be contiguous");
        auto const planned = RequireUInt(wave, "planned_count");
        auto const admitted = RequireUInt(wave, "admitted_count");
        auto const remaining = RequireUInt(wave, "remaining_count");
        if (admitted > planned || remaining != planned - admitted)
            throw std::runtime_error("wave admitted and remaining counts do not match planned_count");
        auto ineligible = RequireUniqueTokens(RequireArray(wave, "ineligible_remaining"), "ineligible_remaining");
        if (ineligible.size() > remaining)
            throw std::runtime_error("wave has more ineligible identities than remaining identities");

        auto const state = RequireString(wave, "state");
        std::string expectedState;
        if (remaining == 0)
            expectedState = "active";
        else if (!ineligible.empty())
            expectedState = "blocked";
        else if (admitted > 0)
            expectedState = "admitting";
        else
            expectedState = "authorized";
        if (state != expectedState)
            throw std::runtime_error("wave state does not match its counts and eligibility");

        Object idempotencyPayload{
            {"realm_id", realmId},
            {"owner_anchor_guid", ownerAnchorGuid},
            {"owner_level", ownerLevel},
            {"schedule_digest", scheduleDigest},
        };
        auto const idempotencyKey = RequireString(wave, "idempotency_key");
        RequireDigest(idempotencyKey, "idempotency_key");
        if (idempotencyKey != Sha256(CanonicalJson(idempotencyPayload)))
            throw std::runtime_error("wave idempotency_key does not match its immutable inputs");
        result.push_back({ownerLevel, remaining, std::move(ineligible)});
    }
    return result;
}

void VerifyPlan(Object root)
{
    RequireExactKeys(
        root,
        {"admission_control", "binding_count", "blocked_identities", "candidate_batch", "decision",
            "earliest_pending_wave", "owner_anchor_guid", "owner_state", "pending_wave_levels", "realm_id",
            "reconciliation_digest", "reconciliation_schema", "reconciliation_version",
            "runtime_commit_required", "source_digests", "total_due_count", "wave_states"},
        "admission reconciliation plan");

    if (RequireUInt(root, "reconciliation_schema", 1, 1) != 1)
        throw std::runtime_error("unsupported reconciliation_schema");
    auto const version = RequireString(root, "reconciliation_version");
    RequireToken(version, "reconciliation_version");
    auto const realmId = RequireString(root, "realm_id");
    RequireToken(realmId, "realm_id");
    if (!RequireBool(root, "runtime_commit_required"))
        throw std::runtime_error("runtime_commit_required must be true");

    auto const expectedDigest = RequireString(root, "reconciliation_digest");
    RequireDigest(expectedDigest, "reconciliation_digest");
    root.erase("reconciliation_digest");
    if (Sha256(CanonicalJson(root)) != expectedDigest)
        throw std::runtime_error("reconciliation_digest does not match canonical payload");

    auto const& sources = RequireObject(root, "source_digests");
    RequireExactKeys(
        sources,
        {"binding_digest", "manifest_digest", "policy_digest", "schedule_digest", "snapshot_digest"},
        "source_digests");
    for (auto const& name :
        {"binding_digest", "manifest_digest", "policy_digest", "schedule_digest", "snapshot_digest"})
        RequireDigest(RequireString(sources, name), name);
    auto const scheduleDigest = RequireString(sources, "schedule_digest");

    auto const ownerAnchorGuid = RequireUInt(root, "owner_anchor_guid", 1);
    auto const& owner = RequireObject(root, "owner_state");
    RequireExactKeys(
        owner,
        {"current_epoch", "newly_authorized_levels", "observed_owner_level",
            "previous_highest_owner_level_seen", "resulting_highest_owner_level_seen"},
        "owner_state");
    auto const observed = RequireUInt(owner, "observed_owner_level", 1, 80);
    auto const previous = RequireUInt(owner, "previous_highest_owner_level_seen", 0, 80);
    auto const resulting = RequireUInt(owner, "resulting_highest_owner_level_seen", 1, 80);
    if (resulting != std::max(observed, previous) || RequireUInt(owner, "current_epoch", 1, 80) != resulting)
        throw std::runtime_error("owner high-water and current epoch are not monotonic");
    auto const newlyAuthorized = RequireIncreasingLevels(
        RequireArray(owner, "newly_authorized_levels"), "newly_authorized_levels");
    std::vector<std::uint64_t> expectedNewlyAuthorized;
    if (observed > previous)
        for (auto level = previous + 1; level <= observed; ++level)
            expectedNewlyAuthorized.push_back(level);
    if (newlyAuthorized != expectedNewlyAuthorized)
        throw std::runtime_error("newly_authorized_levels does not match the owner high-water transition");

    auto const& control = RequireObject(root, "admission_control");
    RequireExactKeys(
        control,
        {"admissions_paused", "available_creation_slots", "creation_batch_size", "pause_reason"},
        "admission_control");
    auto const paused = RequireBool(control, "admissions_paused");
    auto const slots = RequireUInt(control, "available_creation_slots");
    auto const batchSize = RequireUInt(control, "creation_batch_size", 1);
    auto const& pauseReason = control.at("pause_reason");
    if (paused)
    {
        if (!pauseReason.is_string())
            throw std::runtime_error("pause_reason must be a token when admissions are paused");
        RequireToken(std::string(pauseReason.as_string()), "pause_reason");
    }
    else if (!pauseReason.is_null())
        throw std::runtime_error("pause_reason must be null when admissions are not paused");

    RequireUInt(root, "binding_count");
    auto const declaredTotalDue = RequireUInt(root, "total_due_count");
    auto const waves = VerifyWaveStates(root, resulting, realmId, ownerAnchorGuid, scheduleDigest);
    std::vector<std::uint64_t> derivedPending;
    std::uint64_t derivedTotalDue = 0;
    for (auto const& wave : waves)
    {
        if (wave.remainingCount)
            derivedPending.push_back(wave.ownerLevel);
        derivedTotalDue += wave.remainingCount;
    }
    auto const declaredPending = RequireIncreasingLevels(
        RequireArray(root, "pending_wave_levels"), "pending_wave_levels");
    if (declaredPending != derivedPending || declaredTotalDue != derivedTotalDue)
        throw std::runtime_error("pending waves or total_due_count do not match wave_states");

    auto const& earliestValue = root.at("earliest_pending_wave");
    std::uint64_t earliest = 0;
    if (derivedPending.empty())
    {
        if (!earliestValue.is_null())
            throw std::runtime_error("earliest_pending_wave must be null when no work is pending");
    }
    else
    {
        Object wrapper{{"earliest", earliestValue}};
        earliest = RequireUInt(wrapper, "earliest", 1, 80);
        if (earliest != derivedPending.front())
            throw std::runtime_error("earliest_pending_wave is not the first pending wave");
    }

    std::vector<std::string> expectedBlocked;
    std::uint64_t earliestRemaining = 0;
    if (earliest)
    {
        auto const& wave = waves.at(static_cast<std::size_t>(earliest - 1));
        earliestRemaining = wave.remainingCount;
        expectedBlocked = wave.ineligible;
    }
    auto const blocked = RequireUniqueTokens(RequireArray(root, "blocked_identities"), "blocked_identities");
    if (blocked != expectedBlocked)
        throw std::runtime_error("blocked_identities does not match the earliest pending wave");

    auto const decision = RequireString(root, "decision");
    std::string expectedDecision;
    if (!earliest)
        expectedDecision = "noop";
    else if (!expectedBlocked.empty())
        expectedDecision = "blocked_ineligible";
    else if (paused)
        expectedDecision = "paused";
    else if (slots == 0)
        expectedDecision = "capacity_hold";
    else
        expectedDecision = "admit_batch";
    if (decision != expectedDecision)
        throw std::runtime_error("decision does not match derived admission state");

    auto const& candidates = RequireArray(root, "candidate_batch");
    if (decision != "admit_batch" && !candidates.empty())
        throw std::runtime_error("non-admit decisions must not contain candidates");
    auto const limit = std::min({batchSize, slots, earliestRemaining});
    if (decision == "admit_batch" && (candidates.empty() || candidates.size() != limit))
        throw std::runtime_error("candidate batch cardinality does not match the bounded earliest-wave limit");

    std::set<std::string> candidateIdentities;
    std::uint64_t priorOrdinal = 0;
    for (auto const& value : candidates)
    {
        if (!value.is_object())
            throw std::runtime_error("candidate_batch must contain objects");
        auto const& candidate = value.as_object();
        RequireExactKeys(
            candidate,
            {"creation_wave_owner_level", "manifest_identity", "wave_ordinal"},
            "candidate");
        auto identity = RequireString(candidate, "manifest_identity");
        RequireToken(identity, "manifest_identity");
        if (!candidateIdentities.emplace(identity).second)
            throw std::runtime_error("candidate identities must be unique");
        if (RequireUInt(candidate, "creation_wave_owner_level", 1, 80) != earliest)
            throw std::runtime_error("candidate belongs to a non-earliest wave");
        auto const ordinal = RequireUInt(candidate, "wave_ordinal", 1);
        if (ordinal <= priorOrdinal)
            throw std::runtime_error("candidate wave ordinals must be strictly increasing");
        priorOrdinal = ordinal;
    }
}
} // namespace

int main(int argc, char** argv)
{
    if (argc != 2)
    {
        std::cerr << "usage: admission_reconciliation_verifier <plan.json>\n";
        return 2;
    }
    try
    {
        auto parsed = json::parse(ReadFile(argv[1]));
        if (!parsed.is_object())
            throw std::runtime_error("admission reconciliation plan must be a JSON object");
        VerifyPlan(parsed.as_object());
        std::cout << "PASS: C++ accepted the canonical fail-closed admission reconciliation plan\n";
        return 0;
    }
    catch (std::exception const& error)
    {
        std::cerr << "FAIL: " << error.what() << '\n';
        return 1;
    }
}
