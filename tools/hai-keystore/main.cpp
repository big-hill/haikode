/*
 * hai-keystore — lagrer API-nøkler i Haikus innebygde nøkkelring
 * (BKeyStore/BPasswordKey, samme mekanisme som WebPositive bruker).
 *
 * Brukes av Python-CLI-en "hai" via subprocess.
 *
 * Bruk:
 *   hai-keystore set-stdin <identifier>      (hemmelighet leses fra stdin)
 *   hai-keystore get <identifier>            (secret på stdout, exit 0; exit 1 hvis ikke funnet)
 *   hai-keystore remove <identifier>         (exit 0; exit 1 hvis ikke funnet)
 *   hai-keystore list                        (identifiers, én per linje)
 *   hai-keystore set <identifier> <secret>   (UTFASET — se under)
 *
 * "set" tar hemmeligheten som argument, og argv er lesbart for alle brukere
 * på maskinen via ps. Verbet beholdes én utgivelse til for skript som
 * fortsatt bruker det, men advarer på stderr; "set-stdin" er erstatningen.
 *
 * Exit-koder: 0 = suksess, 1 = ikke funnet, 2 = feil bruk,
 *             3 = keystore-feil eller timeout (f.eks. GUI-godkjenningsdialog
 *                 som venter på maskinens fysiske skjerm).
 */

#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <string>

#include <Application.h>
#include <Key.h>
#include <KeyStore.h>
#include <String.h>

// Alle nøkler denne CLI-en eier merkes med denne secondaryIdentifier,
// slik at "list" kun viser våre egne nøkler.
static const char* kSecondaryIdentifier = "hai";

// Failsafe: første aksess mot keystore_server kan utløse en
// GUI-godkjenningsdialog på maskinens fysiske skjerm. Kjøres kommandoen
// over SSH ville den ellers henge for alltid.
static const unsigned int kTimeoutSeconds = 10;


static void
timeout_handler(int)
{
	static const char message[] =
		"hai-keystore: timed out waiting for keystore_server "
		"(an approval dialog may be waiting on the machine's screen)\n";
	// Kun async-signal-sikre kall her.
	write(STDERR_FILENO, message, sizeof(message) - 1);
	_exit(3);
}


static void
print_usage(FILE* out)
{
	fprintf(out,
		"usage: hai-keystore set-stdin <identifier>   (secret on stdin)\n"
		"       hai-keystore get <identifier>\n"
		"       hai-keystore remove <identifier>\n"
		"       hai-keystore list\n"
		"       hai-keystore set <identifier> <secret>   (deprecated)\n");
}


static bool
is_not_found(status_t status)
{
	return status == B_ENTRY_NOT_FOUND || status == B_NAME_NOT_FOUND;
}


// Leser hele stdin. Hemmeligheten sendes denne veien nettopp for å holde den
// ute av argv; én avsluttende linjeskift strippes slik at `printf '%s\n'` og
// `printf '%s'` gir samme nøkkel.
static status_t
read_secret_from_stdin(std::string& out)
{
	char buffer[1024];
	size_t read = 0;
	while ((read = fread(buffer, 1, sizeof(buffer), stdin)) > 0)
		out.append(buffer, read);
	if (ferror(stdin))
		return B_IO_ERROR;
	if (!out.empty() && out[out.size() - 1] == '\n')
		out.erase(out.size() - 1);
	if (!out.empty() && out[out.size() - 1] == '\r')
		out.erase(out.size() - 1);
	return out.empty() ? B_BAD_VALUE : B_OK;
}


// Erstatter en eventuell eksisterende nøkkel med samme identifier.
static int
store_secret(BKeyStore& keyStore, const char* identifier, const char* secret)
{
	BPasswordKey existing;
	status_t status = keyStore.GetKey(B_KEY_TYPE_PASSWORD, identifier,
		kSecondaryIdentifier, existing);
	if (status == B_OK) {
		status = keyStore.RemoveKey(existing);
		if (status != B_OK) {
			fprintf(stderr, "hai-keystore: failed to replace existing "
				"key \"%s\": %s\n", identifier, strerror(status));
			return 3;
		}
	} else if (!is_not_found(status)) {
		fprintf(stderr, "hai-keystore: keystore lookup failed for "
			"\"%s\": %s\n", identifier, strerror(status));
		return 3;
	}

	BPasswordKey key(secret, B_KEY_PURPOSE_GENERIC, identifier,
		kSecondaryIdentifier);
	status = keyStore.AddKey(key);
	if (status != B_OK) {
		fprintf(stderr, "hai-keystore: failed to store key \"%s\": %s\n",
			identifier, strerror(status));
		return 3;
	}
	return 0;
}


int
main(int argc, char** argv)
{
	if (argc < 2) {
		print_usage(stderr);
		return 2;
	}

	signal(SIGALRM, timeout_handler);
	alarm(kTimeoutSeconds);

	// keystore_server identifiserer klienter via registrar; uten en
	// registrert BApplication svarer serveren med B_BAD_TEAM_ID
	// ("Operation on invalid team").
	BApplication app("application/x-vnd.hai-keystore");

	BKeyStore keyStore;
	const char* command = argv[1];

	if (strcmp(command, "set-stdin") == 0) {
		if (argc != 3) {
			print_usage(stderr);
			return 2;
		}
		std::string secret;
		status_t status = read_secret_from_stdin(secret);
		if (status != B_OK) {
			fprintf(stderr, "hai-keystore: no secret on stdin for \"%s\"\n",
				argv[2]);
			return 2;
		}
		int result = store_secret(keyStore, argv[2], secret.c_str());
		// Ikke la hemmeligheten ligge igjen i prosessens minne lenger enn
		// nødvendig.
		if (!secret.empty())
			memset(&secret[0], 0, secret.size());
		return result;
	}

	if (strcmp(command, "set") == 0) {
		if (argc != 4) {
			print_usage(stderr);
			return 2;
		}
		fprintf(stderr, "hai-keystore: warning: \"set\" puts the secret in "
			"argv, where any user can read it with ps. Use \"set-stdin\".\n");
		const char* identifier = argv[2];
		char* secret = argv[3];

		int result = store_secret(keyStore, identifier, secret);
		// Overskriv argumentet så snart nøkkelen er lagret. Det lukker ikke
		// hullet — ps kan ha lest argv allerede — men det forkorter vinduet.
		memset(secret, 0, strlen(secret));
		return result;
	}

	if (strcmp(command, "get") == 0) {
		if (argc != 3) {
			print_usage(stderr);
			return 2;
		}
		const char* identifier = argv[2];

		BPasswordKey key;
		status_t status = keyStore.GetKey(B_KEY_TYPE_PASSWORD, identifier,
			kSecondaryIdentifier, key);
		if (is_not_found(status)) {
			fprintf(stderr, "hai-keystore: no key stored for \"%s\"\n",
				identifier);
			return 1;
		}
		if (status != B_OK) {
			fprintf(stderr, "hai-keystore: keystore lookup failed for "
				"\"%s\": %s\n", identifier, strerror(status));
			return 3;
		}

		const char* password = key.Password();
		fputs(password != NULL ? password : "", stdout);
		fputc('\n', stdout);
		return 0;
	}

	if (strcmp(command, "remove") == 0) {
		if (argc != 3) {
			print_usage(stderr);
			return 2;
		}
		const char* identifier = argv[2];

		// Hent den eksakte nøkkelen først, slik at RemoveKey matcher.
		BPasswordKey key;
		status_t status = keyStore.GetKey(B_KEY_TYPE_PASSWORD, identifier,
			kSecondaryIdentifier, key);
		if (is_not_found(status)) {
			fprintf(stderr, "hai-keystore: no key stored for \"%s\"\n",
				identifier);
			return 1;
		}
		if (status != B_OK) {
			fprintf(stderr, "hai-keystore: keystore lookup failed for "
				"\"%s\": %s\n", identifier, strerror(status));
			return 3;
		}

		status = keyStore.RemoveKey(key);
		if (is_not_found(status)) {
			fprintf(stderr, "hai-keystore: no key stored for \"%s\"\n",
				identifier);
			return 1;
		}
		if (status != B_OK) {
			fprintf(stderr, "hai-keystore: failed to remove key \"%s\": %s\n",
				identifier, strerror(status));
			return 3;
		}
		return 0;
	}

	if (strcmp(command, "list") == 0) {
		if (argc != 2) {
			print_usage(stderr);
			return 2;
		}

		uint32 cookie = 0;
		while (true) {
			BPasswordKey key;
			status_t status = keyStore.GetNextKey(B_KEY_TYPE_PASSWORD,
				B_KEY_PURPOSE_GENERIC, cookie, key);
			if (status != B_OK)
				break;

			const char* secondary = key.SecondaryIdentifier();
			if (secondary != NULL
				&& strcmp(secondary, kSecondaryIdentifier) == 0) {
				printf("%s\n", key.Identifier());
			}
		}
		return 0;
	}

	print_usage(stderr);
	return 2;
}
