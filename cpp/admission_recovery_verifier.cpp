// Standalone C++ proof of the per-identity admission-recovery wire contract.
// This is not yet an AzerothCore module or database integration.

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

std::string ReadFile(std::string const& path)
{
    std::ifstream input(path, std::ios::binary);
    if (!input)
        throw std::runtime_error("cannot open recovery plan: " + path);
    std::ostringstream content;
    content << input.rdbuf();
    return content.str();
}

void Exact(Object const& object, std::set<std::string> const& expected, std::string_view name)
{
    std::set<std::string> actual;
    for (auto const& item : object)
        actual.emplace(item.key());
    if (actual != expected)
        throw std::runtime_error(std::string(name) + " has missing or unknown fields");
}

Object const& ObjectAt(Object const& object, std::string_view key)
{
    auto const& value = object.at(key);
    if (!value.is_object())
        throw std::runtime_error(std::string(key) + " must be an object");
    return value.as_object();
}

std::string StringAt(Object const& object, std::string_view key)
{
    auto const& value = object.at(key);
    if (!value.is_string())
        throw std::runtime_error(std::string(key) + " must be a string");
    return std::string(value.as_string());
}

std::uint64_t UIntAt(Object const& object, std::string_view key, std::uint64_t minimum = 0,
    std::uint64_t maximum = UINT64_MAX)
{
    auto const& value = object.at(key);
    std::uint64_t result;
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

bool BoolAt(Object const& object, std::string_view key)
{
    auto const& value = object.at(key);
    if (!value.is_bool())
        throw std::runtime_error(std::string(key) + " must be a boolean");
    return value.as_bool();
}

void Token(std::string const& value, std::string_view name)
{
    static std::regex const token("^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$");
    if (!std::regex_match(value, token))
        throw std::runtime_error(std::string(name) + " is not a stable token");
}

void Digest(std::string const& value, std::string_view name)
{
    if (value.size() != 64 || !std::ranges::all_of(value, [](unsigned char ch)
        {
            return std::isdigit(ch) || (ch >= 'a' && ch <= 'f');
        }))
        throw std::runtime_error(std::string(name) + " is not lowercase SHA-256");
}

std::string Canonical(json::value const& value)
{
    if (value.is_object())
    {
        auto const& object = value.as_object();
        std::vector<std::string> keys;
        for (auto const& item : object)
            keys.emplace_back(item.key());
        std::ranges::sort(keys);
        std::string result = "{";
        for (std::size_t index = 0; index < keys.size(); ++index)
        {
            if (index)
                result += ',';
            result += json::serialize(json::string(keys[index])) + ':' + Canonical(object.at(keys[index]));
        }
        return result + '}';
    }
    if (value.is_array())
    {
        std::string result = "[";
        auto const& array = value.as_array();
        for (std::size_t index = 0; index < array.size(); ++index)
        {
            if (index)
                result += ',';
            result += Canonical(array[index]);
        }
        return result + ']';
    }
    if (value.is_double())
        throw std::runtime_error("floating-point numbers are forbidden in a recovery plan");
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
    std::ostringstream result;
    result << std::hex << std::setfill('0');
    for (unsigned int index = 0; index < length; ++index)
        result << std::setw(2) << static_cast<unsigned int>(bytes[index]);
    return result.str();
}

struct OptionalAccount
{
    bool present = false;
    std::uint64_t id = 0;
    std::string name;
    std::uint64_t characterDatabaseNumchars = 0;
    bool realmcharactersRowPresent = false;
    std::uint64_t realmcharactersNumchars = 0;
};

struct Appearance
{
    std::uint64_t face = 0;
    std::uint64_t facialHair = 0;
    std::uint64_t gender = 0;
    std::uint64_t hairColor = 0;
    std::uint64_t hairStyle = 0;
    std::uint64_t skin = 0;

    bool operator==(Appearance const&) const = default;
};

struct OptionalCharacter
{
    bool present = false;
    std::uint64_t guid = 0;
    std::uint64_t account = 0;
    std::string name;
    std::uint64_t race = 0;
    std::uint64_t classId = 0;
    Appearance appearance;
    std::uint64_t level = 0;
    std::uint64_t xp = 0;
    std::uint64_t totalTime = 0;
    std::uint64_t levelTime = 0;
    std::uint64_t questRows = 0;
};

struct OptionalRndbot
{
    bool present = false;
    std::uint64_t account = 0;
    std::uint64_t type = 0;
};

struct ExpectedDecision
{
    std::string action;
    std::string toState;
    std::string domain;
    bool readBack;
    bool mutateLedger;
};

void VerifyPlan(Object root)
{
    Exact(root,
        {"decision", "expected_identity", "external_observation", "ledger_observation",
            "manifest_identity", "realm_id", "recovery_digest", "recovery_schema", "recovery_version",
            "runtime_commit_required", "source_digests"},
        "recovery plan");
    if (UIntAt(root, "recovery_schema", 1, 1) != 1)
        throw std::runtime_error("unsupported recovery_schema");
    Token(StringAt(root, "recovery_version"), "recovery_version");
    Token(StringAt(root, "realm_id"), "realm_id");
    Token(StringAt(root, "manifest_identity"), "manifest_identity");

    auto expectedDigest = StringAt(root, "recovery_digest");
    Digest(expectedDigest, "recovery_digest");
    root.erase("recovery_digest");
    if (Sha256(Canonical(root)) != expectedDigest)
        throw std::runtime_error("recovery_digest does not match canonical payload");

    auto const& sources = ObjectAt(root, "source_digests");
    Exact(sources, {"manifest_digest", "reconciliation_digest", "snapshot_digest"}, "source_digests");
    for (auto const& key : {"manifest_digest", "reconciliation_digest", "snapshot_digest"})
        Digest(StringAt(sources, key), key);

    auto const& expected = ObjectAt(root, "expected_identity");
    Exact(expected,
        {"account_manifest_size", "account_name", "appearance", "character_name", "class", "creation_wave_owner_level", "race",
            "wave_idempotency_key", "wave_ordinal"},
        "expected_identity");
    auto const expectedAccountManifestSize = UIntAt(expected, "account_manifest_size", 1, 9);
    auto const expectedAccountName = StringAt(expected, "account_name");
    auto const expectedCharacterName = StringAt(expected, "character_name");
    Token(expectedAccountName, "account_name");
    Token(expectedCharacterName, "character_name");
    auto const expectedClass = UIntAt(expected, "class", 1, 11);
    auto const expectedRace = UIntAt(expected, "race", 1, 11);
    auto const& expectedAppearanceValue = ObjectAt(expected, "appearance");
    Exact(expectedAppearanceValue,
        {"face", "facial_hair", "gender", "hair_color", "hair_style", "outfit_id", "skin"},
        "expected appearance");
    Appearance const expectedAppearance{
        UIntAt(expectedAppearanceValue, "face", 0, 255),
        UIntAt(expectedAppearanceValue, "facial_hair", 0, 255),
        UIntAt(expectedAppearanceValue, "gender", 0, 1),
        UIntAt(expectedAppearanceValue, "hair_color", 0, 255),
        UIntAt(expectedAppearanceValue, "hair_style", 0, 255),
        UIntAt(expectedAppearanceValue, "skin", 0, 255),
    };
    if (UIntAt(expectedAppearanceValue, "outfit_id", 0, 0) != 0)
        throw std::runtime_error("outfit_id must be zero for the pinned core");
    UIntAt(expected, "creation_wave_owner_level", 1, 80);
    UIntAt(expected, "wave_ordinal", 1);
    auto const waveKey = StringAt(expected, "wave_idempotency_key");
    Digest(waveKey, "wave_idempotency_key");

    std::string state = "absent";
    std::uint64_t generation = 0;
    std::uint64_t ledgerAccount = 0;
    std::uint64_t ledgerCharacter = 0;
    auto const& ledgerValue = root.at("ledger_observation");
    if (!ledgerValue.is_null())
    {
        if (!ledgerValue.is_object())
            throw std::runtime_error("ledger_observation must be null or an object");
        auto const& ledger = ledgerValue.as_object();
        Exact(ledger,
            {"account_id", "character_guid", "expected_identity", "generation", "manifest_digest", "state",
                "wave_idempotency_key"},
            "ledger_observation");
        state = StringAt(ledger, "state");
        static std::set<std::string> const states = {"reserved", "account_pending", "account_verified",
            "character_pending", "character_verified", "rndbot_pending", "binding_verified", "active",
            "blocked_conflict"};
        if (!states.contains(state))
            throw std::runtime_error("unsupported ledger state");
        generation = UIntAt(ledger, "generation", 1);
        if (StringAt(ledger, "manifest_digest") != StringAt(sources, "manifest_digest") ||
            StringAt(ledger, "wave_idempotency_key") != waveKey)
            throw std::runtime_error("ledger digest or wave key conflicts with expected identity");
        if (Canonical(ledger.at("expected_identity")) != Canonical(expected))
            throw std::runtime_error("ledger expected identity conflicts with manifest");
        if (!ledger.at("account_id").is_null())
            ledgerAccount = UIntAt(ledger, "account_id", 1);
        if (!ledger.at("character_guid").is_null())
            ledgerCharacter = UIntAt(ledger, "character_guid", 1);
    }

    auto const& external = ObjectAt(root, "external_observation");
    Exact(external, {"account", "character", "rndbot"}, "external_observation");
    OptionalAccount account;
    if (!external.at("account").is_null())
    {
        auto const& value = external.at("account").as_object();
        Exact(value,
            {"account_id", "account_name", "character_database_numchars", "realmcharacters_numchars", "realmcharacters_row_present"},
            "account observation");
        auto const rowPresent = BoolAt(value, "realmcharacters_row_present");
        auto const& numcharsValue = value.at("realmcharacters_numchars");
        if (rowPresent != !numcharsValue.is_null())
            throw std::runtime_error("realmcharacters presence and numchars must agree");
        account = {true, UIntAt(value, "account_id", 1), StringAt(value, "account_name"),
            UIntAt(value, "character_database_numchars", 0, 255), rowPresent,
            rowPresent ? UIntAt(value, "realmcharacters_numchars", 0, 255) : 0};
        Token(account.name, "observed account_name");
    }
    OptionalCharacter character;
    if (!external.at("character").is_null())
    {
        auto const& value = external.at("character").as_object();
        Exact(value,
            {"account_id", "appearance", "character_guid", "character_name", "class", "level", "level_time",
                "quest_state", "race", "total_time", "xp"},
            "character observation");
        auto const& appearanceValue = ObjectAt(value, "appearance");
        Exact(appearanceValue, {"face", "facial_hair", "gender", "hair_color", "hair_style", "skin"},
            "character appearance");
        Appearance const appearance{
            UIntAt(appearanceValue, "face", 0, 255),
            UIntAt(appearanceValue, "facial_hair", 0, 255),
            UIntAt(appearanceValue, "gender", 0, 1),
            UIntAt(appearanceValue, "hair_color", 0, 255),
            UIntAt(appearanceValue, "hair_style", 0, 255),
            UIntAt(appearanceValue, "skin", 0, 255),
        };
        auto const& questState = ObjectAt(value, "quest_state");
        Exact(questState,
            {"daily_count", "monthly_count", "rewarded_count", "seasonal_count", "status_count", "weekly_count"},
            "character quest state");
        std::uint64_t questRows = 0;
        for (auto const& key : {"daily_count", "monthly_count", "rewarded_count", "seasonal_count", "status_count",
                 "weekly_count"})
            questRows += UIntAt(questState, key);
        character = {true, UIntAt(value, "character_guid", 1), UIntAt(value, "account_id", 1),
            StringAt(value, "character_name"), UIntAt(value, "race", 1, 11), UIntAt(value, "class", 1, 11),
            appearance, UIntAt(value, "level", 1, 80), UIntAt(value, "xp"), UIntAt(value, "total_time"),
            UIntAt(value, "level_time"), questRows};
        Token(character.name, "observed character_name");
    }
    OptionalRndbot rndbot;
    if (!external.at("rndbot").is_null())
    {
        auto const& value = external.at("rndbot").as_object();
        Exact(value, {"account_id", "account_type"}, "rndbot observation");
        rndbot = {true, UIntAt(value, "account_id", 1), UIntAt(value, "account_type", 0, 2)};
    }

    bool conflict = (account.present && account.name != expectedAccountName) ||
        (character.present && (character.name != expectedCharacterName || character.race != expectedRace ||
                                  character.classId != expectedClass || character.appearance != expectedAppearance ||
                                  character.level != 1 || character.xp != 0 || character.totalTime != 0 ||
                                  character.levelTime != 0 || character.questRows != 0 || !account.present ||
                                  character.account != account.id)) ||
        (rndbot.present && (!account.present || rndbot.account != account.id || rndbot.type != 1)) ||
        (ledgerAccount && (!account.present || ledgerAccount != account.id)) ||
        (ledgerCharacter && (!character.present || ledgerCharacter != character.guid));
    if (account.present)
    {
        if (account.characterDatabaseNumchars > expectedAccountManifestSize)
            conflict = true;
        if (state != "absent" && state != "reserved" && state != "account_pending" &&
            !account.realmcharactersRowPresent)
            conflict = true;
        if (account.realmcharactersRowPresent &&
            account.realmcharactersNumchars != account.characterDatabaseNumchars &&
            state != "account_pending" && state != "character_pending")
            conflict = true;
    }

    ExpectedDecision derived;
    if (conflict)
        derived = {"block_conflict", "blocked_conflict", "director_ledger", false, true};
    else if (state == "absent")
        derived = (character.present || (rndbot.present && !account.present))
            ? ExpectedDecision{"block_conflict", "blocked_conflict", "director_ledger", false, true}
            : ExpectedDecision{"reserve_identity", "reserved", "director_ledger", false, true};
    else if (state == "blocked_conflict")
        derived = {"noop_blocked", state, "none", false, false};
    else if (state == "reserved")
        derived = (character.present || (rndbot.present && !account.present))
            ? ExpectedDecision{"block_conflict", "blocked_conflict", "director_ledger", false, true}
            : ExpectedDecision{"mark_account_pending", "account_pending", "director_ledger", false, true};
    else if (state == "account_pending")
        derived = character.present
            ? ExpectedDecision{"block_conflict", "blocked_conflict", "director_ledger", false, true}
            : !account.present
            ? ExpectedDecision{"create_account", state, "login_database", true, false}
            : !account.realmcharactersRowPresent
            ? ExpectedDecision{"initialize_realmcharacters", state, "login_database", true, false}
            : account.realmcharactersNumchars != account.characterDatabaseNumchars
            ? ExpectedDecision{"refresh_realmcharacters_count", state, "login_database", true, false}
            : ExpectedDecision{"mark_account_verified", "account_verified", "director_ledger", false, true};
    else if (!account.present)
        derived = {"block_conflict", "blocked_conflict", "director_ledger", false, true};
    else if (state == "account_verified")
        derived = character.present
            ? ExpectedDecision{"block_conflict", "blocked_conflict", "director_ledger", false, true}
            : ExpectedDecision{"mark_character_pending", "character_pending", "director_ledger", false, true};
    else if (state == "character_pending")
        derived = !character.present
            ? ExpectedDecision{"create_character", state, "character_database", true, false}
            : account.realmcharactersNumchars != account.characterDatabaseNumchars
            ? ExpectedDecision{"refresh_realmcharacters_count", state, "login_database", true, false}
            : ExpectedDecision{"mark_character_verified", "character_verified", "director_ledger", false, true};
    else if (!character.present)
        derived = {"block_conflict", "blocked_conflict", "director_ledger", false, true};
    else if (state == "character_verified")
        derived = {"mark_rndbot_pending", "rndbot_pending", "director_ledger", false, true};
    else if (state == "rndbot_pending")
        derived = !rndbot.present
            ? ExpectedDecision{"install_rndbot_classification", state, "playerbots_database", true, false}
            : ExpectedDecision{"mark_binding_verified", "binding_verified", "director_ledger", false, true};
    else if (!rndbot.present)
        derived = {"block_conflict", "blocked_conflict", "director_ledger", false, true};
    else if (state == "binding_verified")
        derived = {"activate_identity", "active", "director_ledger", false, true};
    else
        derived = {"noop_active", "active", "none", false, false};

    auto const& decision = ObjectAt(root, "decision");
    Exact(decision,
        {"action", "effect_domain", "expected_generation", "from_state", "read_back_required", "reason",
            "resulting_generation", "to_state"},
        "decision");
    Token(StringAt(decision, "reason"), "decision.reason");
    auto const resultingGeneration = UIntAt(decision, "resulting_generation");
    if (StringAt(decision, "action") != derived.action || StringAt(decision, "from_state") != state ||
        StringAt(decision, "to_state") != derived.toState || StringAt(decision, "effect_domain") != derived.domain ||
        BoolAt(decision, "read_back_required") != derived.readBack ||
        UIntAt(decision, "expected_generation") != generation ||
        resultingGeneration != generation + (derived.mutateLedger ? 1 : 0))
        throw std::runtime_error("decision does not match derived recovery state");
    if (BoolAt(root, "runtime_commit_required") != (derived.action != "noop_active" && derived.action != "noop_blocked"))
        throw std::runtime_error("runtime_commit_required contradicts the derived action");
}
} // namespace

int main(int argc, char** argv)
{
    if (argc != 2)
    {
        std::cerr << "usage: admission_recovery_verifier <plan.json>\n";
        return 2;
    }
    try
    {
        auto parsed = json::parse(ReadFile(argv[1]));
        if (!parsed.is_object())
            throw std::runtime_error("recovery plan must be a JSON object");
        VerifyPlan(parsed.as_object());
        std::cout << "PASS: C++ accepted the canonical fail-closed admission recovery plan\n";
        return 0;
    }
    catch (std::exception const& error)
    {
        std::cerr << "FAIL: " << error.what() << '\n';
        return 1;
    }
}
