#include <stdio.h>

#include "util.h"

/* The largest reading the display can show. */
#define UPPER_BOUND 7

int main(void)
{
	int raw = 12;
	int clamped = clamp(raw, 0);

	printf("clamped=%d\n", clamped);
	return 0;
}
