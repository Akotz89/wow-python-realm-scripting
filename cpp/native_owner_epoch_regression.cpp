#include "Guide/PythonPopulationBridge.h"

static_assert(PlayerbotsPopulation::CappedOwnerEpoch(3, 80, 19) == 3);
static_assert(PlayerbotsPopulation::CappedOwnerEpoch(19, 80, 19) == 19);
static_assert(PlayerbotsPopulation::CappedOwnerEpoch(20, 80, 19) == 19);
static_assert(PlayerbotsPopulation::CappedOwnerEpoch(80, 80, 19) == 19);
static_assert(PlayerbotsPopulation::CappedOwnerEpoch(80, 17, 19) == 17);

int main()
{
    return PlayerbotsPopulation::CappedOwnerEpoch(80, 80, 19) == 19 ? 0 : 1;
}
