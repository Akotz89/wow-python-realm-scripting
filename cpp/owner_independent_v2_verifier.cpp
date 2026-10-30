// Independent proof of the owner-identity-free v2 artifact boundary.
// This is not yet an AzerothCore module or a live database transaction.

#include <boost/json.hpp>
#include <openssl/evp.h>

#include <algorithm>
#include <array>
#include <cctype>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <memory>
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

std::string ReadFile(char const* path)
{
    std::ifstream input(path, std::ios::binary);
    if (!input)
        throw std::runtime_error(std::string("cannot open artifact: ") + path);
    std::ostringstream content;
    content << input.rdbuf();
    if (!input.good() && !input.eof())
        throw std::runtime_error(std::string("cannot read artifact: ") + path);
    return content.str();
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
        throw std::runtime_error("floating-point numbers are forbidden");
    return json::serialize(value);
}

std::string Sha256(std::string_view content)
{
    using Context = std::unique_ptr<EVP_MD_CTX, decltype(&EVP_MD_CTX_free)>;
    Context context(EVP_MD_CTX_new(), EVP_MD_CTX_free);
    if (!context || EVP_DigestInit_ex(context.get(), EVP_sha256(), nullptr) != 1 ||
        EVP_DigestUpdate(context.get(), content.data(), content.size()) != 1)
        throw std::runtime_error("OpenSSL SHA-256 initialization failed");
    std::array<unsigned char, EVP_MAX_MD_SIZE> bytes{};
    unsigned int length = 0;
    if (EVP_DigestFinal_ex(context.get(), bytes.data(), &length) != 1 || length != 32)
        throw std::runtime_error("OpenSSL SHA-256 finalization failed");
    static constexpr char digits[] = "0123456789abcdef";
    std::string result;
    result.reserve(64);
    for (unsigned int index = 0; index < length; ++index)
    {
        result += digits[bytes[index] >> 4];
        result += digits[bytes[index] & 0x0f];
    }
    return result;
}

Object ParseObject(char const* path)
{
    auto value = json::parse(ReadFile(path));
    if (!value.is_object())
        throw std::runtime_error(std::string(path) + " is not a JSON object");
    return value.as_object();
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

void RequireDigest(std::string const& value, std::string_view name)
{
    if (value.size() != 64 || !std::ranges::all_of(value, [](unsigned char character)
        {
            return std::isdigit(character) || (character >= 'a' && character <= 'f');
        }))
        throw std::runtime_error(std::string(name) + " is not lowercase SHA-256");
}

void VerifyArtifactDigest(Object const& root, std::string_view field)
{
    auto expected = RequireString(root, field);
    RequireDigest(expected, field);
    Object payload = root;
    payload.erase(field);
    if (Sha256(CanonicalJson(payload)) != expected)
        throw std::runtime_error(std::string(field) + " does not match artifact content");
}

void RejectOwnerIdentity(json::value const& value, std::string_view artifact)
{
    static std::set<std::string> const forbidden{
        "owner_account_id", "owner_anchor_guid", "owner_character_guid", "owner_character_name"};
    if (value.is_object())
    {
        for (auto const& item : value.as_object())
        {
            if (forbidden.contains(std::string(item.key())))
                throw std::runtime_error(std::string(artifact) + " embeds owner identity key " + item.key_c_str());
            RejectOwnerIdentity(item.value(), artifact);
        }
    }
    else if (value.is_array())
    {
        for (auto const& item : value.as_array())
            RejectOwnerIdentity(item, artifact);
    }
}

std::string WaveKey(std::string const& realmId, std::uint64_t ownerLevel, std::string const& scheduleDigest)
{
    Object payload{
        {"realm_id", realmId},
        {"owner_level", ownerLevel},
        {"schedule_digest", scheduleDigest},
    };
    return Sha256(CanonicalJson(payload));
}

void VerifyImmutableArtifacts(Object const& policy, Object const& snapshot, Object const& installation)
{
    VerifyArtifactDigest(policy, "policy_digest");
    VerifyArtifactDigest(snapshot, "snapshot_digest");
    VerifyArtifactDigest(installation, "installation_digest");
    RejectOwnerIdentity(policy, "policy");
    RejectOwnerIdentity(snapshot, "snapshot");
    RejectOwnerIdentity(installation, "installation");
    if (RequireUInt(policy, "policy_schema") != 2 || RequireUInt(snapshot, "snapshot_schema") != 2 ||
        RequireUInt(installation, "installation_schema") != 2)
        throw std::runtime_error("all immutable artifacts must use schema 2");

    auto const& schedule = RequireObject(policy, "schedule");
    VerifyArtifactDigest(schedule, "schedule_digest");
    if (RequireUInt(schedule, "schema_version") != 2)
        throw std::runtime_error("schedule must use schema 2");
    auto realmId = RequireString(schedule, "realm_id");
    auto scheduleDigest = RequireString(schedule, "schedule_digest");
    RequireDigest(scheduleDigest, "schedule_digest");
    if (RequireString(installation, "realm_id") != realmId)
        throw std::runtime_error("installation realm does not match schedule");
    auto const& installDigests = RequireObject(installation, "source_digests");
    auto const& snapshotDigests = RequireObject(snapshot, "source_digests");
    if (RequireString(installDigests, "schedule_digest") != scheduleDigest ||
        RequireString(snapshotDigests, "schedule_digest") != scheduleDigest)
        throw std::runtime_error("immutable artifacts do not share one schedule digest");

    auto const& ownerState = RequireObject(installation, "owner_state");
    if (RequireString(ownerState, "binding_state") != "unbound" || ownerState.size() != 4 ||
        RequireUInt(ownerState, "highest_owner_level_seen") != 1 || RequireUInt(ownerState, "current_epoch") != 1 ||
        RequireUInt(ownerState, "generation") != 1)
        throw std::runtime_error("installation owner state is not exactly unbound generation 1");

    auto maximumLevel = RequireUInt(schedule, "maximum_owner_level", 1);
    auto const& waves = RequireArray(installation, "waves");
    if (waves.size() != maximumLevel)
        throw std::runtime_error("installation wave count does not match maximum owner level");
    for (std::size_t index = 0; index < waves.size(); ++index)
    {
        if (!waves[index].is_object())
            throw std::runtime_error("installation waves must be objects");
        auto const& wave = waves[index].as_object();
        auto ownerLevel = RequireUInt(wave, "owner_level", 1);
        if (ownerLevel != index + 1)
            throw std::runtime_error("installation wave levels are not contiguous");
        if (RequireString(wave, "wave_idempotency_key") != WaveKey(realmId, ownerLevel, scheduleDigest))
            throw std::runtime_error("wave idempotency key contains wrong inputs");
        auto expectedState = ownerLevel == 1 ? "authorized" : "planned";
        if (RequireString(wave, "initial_state") != expectedState)
            throw std::runtime_error("only wave one may be initially authorized");
    }
}

void VerifyBinding(Object const& binding, Object const& installation)
{
    VerifyArtifactDigest(binding, "transition_digest");
    if (RequireUInt(binding, "binding_schema") != 2)
        throw std::runtime_error("owner binding must use schema 2");
    auto operation = RequireString(binding, "operation");
    if (operation != "bind" && operation != "rebind")
        throw std::runtime_error("owner binding operation is invalid");
    auto const& authorized = RequireArray(binding, "authorized_wave_levels");
    if (!authorized.empty())
        throw std::runtime_error("owner binding must authorize zero waves");
    auto const& digests = RequireObject(binding, "source_digests");
    if (RequireString(digests, "installation_digest") != RequireString(installation, "installation_digest"))
        throw std::runtime_error("owner binding does not bind the installation");
    auto const& selected = RequireObject(binding, "selected_owner");
    RequireUInt(selected, "account_id", 1);
    RequireUInt(selected, "character_guid", 1);
    auto expected = RequireUInt(binding, "expected_generation", 1);
    if (RequireUInt(binding, "next_generation", 1) != expected + 1)
        throw std::runtime_error("owner binding generation is not a single compare-and-swap step");
}

void VerifyReconciliation(Object const& reconciliation, Object const& policy)
{
    VerifyArtifactDigest(reconciliation, "reconciliation_digest");
    if (RequireUInt(reconciliation, "reconciliation_schema") != 2)
        throw std::runtime_error("reconciliation must use schema 2");
    auto const& schedule = RequireObject(policy, "schedule");
    auto realmId = RequireString(schedule, "realm_id");
    auto scheduleDigest = RequireString(schedule, "schedule_digest");
    if (RequireString(reconciliation, "realm_id") != realmId ||
        RequireString(RequireObject(reconciliation, "source_digests"), "schedule_digest") != scheduleDigest)
        throw std::runtime_error("reconciliation does not bind the immutable schedule");

    auto const& owner = RequireObject(reconciliation, "owner_state");
    auto bindingState = RequireString(owner, "binding_state");
    auto resultingHighWater = RequireUInt(owner, "resulting_highest_owner_level_seen", 1);
    auto const& newlyAuthorized = RequireArray(owner, "newly_authorized_levels");
    if (bindingState == "unbound")
    {
        if (!owner.at("owner_account_id").is_null() || !owner.at("owner_character_guid").is_null() ||
            !owner.at("observed_owner_level").is_null() || resultingHighWater != 1 || !newlyAuthorized.empty())
            throw std::runtime_error("unbound reconciliation can authorize only wave one and no owner identity");
    }
    else if (bindingState == "bound")
    {
        RequireUInt(owner, "owner_account_id", 1);
        RequireUInt(owner, "owner_character_guid", 1);
        RequireUInt(owner, "observed_owner_level", 1);
    }
    else
        throw std::runtime_error("reconciliation binding state is invalid");

    auto const& waveStates = RequireArray(reconciliation, "wave_states");
    if (waveStates.size() != resultingHighWater)
        throw std::runtime_error("reconciliation wave states do not match resulting high-water");
    for (std::size_t index = 0; index < waveStates.size(); ++index)
    {
        if (!waveStates[index].is_object())
            throw std::runtime_error("reconciliation wave states must be objects");
        auto const& wave = waveStates[index].as_object();
        auto ownerLevel = RequireUInt(wave, "owner_level", 1);
        if (ownerLevel != index + 1 ||
            RequireString(wave, "idempotency_key") != WaveKey(realmId, ownerLevel, scheduleDigest))
            throw std::runtime_error("reconciliation wave key or order is invalid");
    }
    for (auto const& value : RequireArray(reconciliation, "candidate_batch"))
    {
        if (!value.is_object())
            throw std::runtime_error("candidate batch must contain objects");
        auto level = RequireUInt(value.as_object(), "creation_wave_owner_level", 1);
        if (level > resultingHighWater || (bindingState == "unbound" && level != 1))
            throw std::runtime_error("candidate batch contains an unauthorized wave");
    }
}
}

int main(int argc, char** argv)
{
    if (argc != 5 && argc != 6)
    {
        std::cerr << "usage: owner_independent_v2_verifier POLICY SNAPSHOT INSTALLATION RECONCILIATION [BINDING]\n";
        return 2;
    }
    try
    {
        auto policy = ParseObject(argv[1]);
        auto snapshot = ParseObject(argv[2]);
        auto installation = ParseObject(argv[3]);
        VerifyImmutableArtifacts(policy, snapshot, installation);
        VerifyReconciliation(ParseObject(argv[4]), policy);
        if (argc == 6)
            VerifyBinding(ParseObject(argv[5]), installation);
        std::cout << "PASS: v2 immutable artifacts are owner-independent and wave one alone is authorized\n";
        return 0;
    }
    catch (std::exception const& error)
    {
        std::cerr << "FAIL: " << error.what() << '\n';
        return 1;
    }
}
