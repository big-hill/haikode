#include "util.h"

int clamp(int value, int low, int high)
{
	if (value < low)
		return low;
	if (value > high)
		return high;
	return value;
}
