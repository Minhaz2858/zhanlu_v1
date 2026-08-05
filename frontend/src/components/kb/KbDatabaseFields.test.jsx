/**
 * parseConnString — URL-encoding regression suite.
 *
 * Why this exists
 * ---------------
 * When a user pastes a CLI connection string copied from a SQLAlchemy
 * URL (e.g. ``--password=0Gg.B7c2%40tcX_jne6FMh``) the parser used to
 * extract the password verbatim, with the URL-encoded characters still
 * encoded. The backend then re-encoded it (so ``%40`` became ``%2540``)
 * and MySQL received the wrong password.
 *
 * The parser now URL-decodes every value it extracts, so a pasted
 * connection string round-trips through the wizard the same way the
 * source URL did.
 *
 * These tests pin the contract:
 *   * ``--password=%40...`` decodes to ``@...``
 *   * ``-h`` / ``-u`` / ``-P`` / ``-d`` / positional args all decode
 *   * ``-pVALUE`` (MySQL attached password) decodes
 *   * Quoted values (``--password="abc%40def"``) still strip quotes
 *   * Malformed escapes fall back to the raw token (no throw)
 *   * SQLAlchemy URLs (``mysql+pymysql://...``) parse into the same
 *     field shape as CLI syntax — including URL-decoded userinfo.
 *   * The number of existing "happy path" assertions stays the same
 */

import { describe, it, expect } from 'vitest';
import { parseConnString, parseSqlalchemyUrl } from '@/components/kb/KbDatabaseFields';

describe('parseConnString — URL decoding', () => {
  it("decodes %40 (@) in --password= so the wizard sends the real password", () => {
    // The exact failure case from the bug report.
    const result = parseConnString(
      'mysql -h 10.10.10.49 -P 3306 -u root --password=0Gg.B7c2%40tcX_jne6FMh aipdp_data_warehouse_prod'
    );
    expect(result).toEqual({
      db_type: 'mysql',
      host: '10.10.10.49',
      port: 3306,
      username: 'root',
      password: '0Gg.B7c2@tcX_jne6FMh',
      database_name: 'aipdp_data_warehouse_prod',
    });
  });

  it('decodes %20 (space) in --host=', () => {
    const result = parseConnString('mysql -h "my%20db.example.com" -P 3306 -u root -psecret db1');
    expect(result.host).toBe('my db.example.com');
  });

  it('decodes %2B (+) in -pVALUE attached password', () => {
    // MySQL accepts ``-pSynexia+123`` as the password; users often
    // copy this with the + percent-encoded when the source is a URL.
    const result = parseConnString('mysql -h 10.0.0.1 -P 3306 -u root -pp%2Bssw0rd db1');
    expect(result.password).toBe('p+ssw0rd');
  });

  it('decodes %23 (#) in -u VALUE', () => {
    const result = parseConnString('mysql -h 10.0.0.1 -P 3306 -u user%23admin -psecret db1');
    expect(result.username).toBe('user#admin');
  });

  it('decodes %2D (-) in positional database name', () => {
    // Use --password= so -p ambiguity doesn't interfere.
    const result = parseConnString('mysql -h h -P 3306 -u u --password=p my%2Ddb');
    expect(result.database_name).toBe('my-db');
  });

  it('strips surrounding quotes before decoding', () => {
    // Both single and double quotes should be stripped, then the
    // remaining text should be URL-decoded.
    const dq = parseConnString('mysql -h h -P 3306 -u u --password="a%40b" d');
    const sq = parseConnString("mysql -h h -P 3306 -u u --password='a%40b' d");
    expect(dq.password).toBe('a@b');
    expect(sq.password).toBe('a@b');
  });

  it('falls back to the raw token on malformed escape sequences', () => {
    // ``%ZZ`` is not a valid percent-escape; decodeURIComponent throws.
    // The parser must swallow that and keep the original token so the
    // user can still see/edit the value in the form fields.
    const result = parseConnString('mysql -h h -P 3306 -u u --password=bad%ZZpass d');
    expect(result.password).toBe('bad%ZZpass');
  });

  it('decodes %25 (%) when the literal percent is itself encoded', () => {
    // ``100%2B25%25`` (raw "100+25%") should round-trip to "100+25%".
    const result = parseConnString('mysql -h h -P 3306 -u u --password=100%2B25%25 d');
    expect(result.password).toBe('100+25%');
  });
});

describe('parseConnString — regression: existing behaviour still holds', () => {
  it('returns null on empty input', () => {
    expect(parseConnString('')).toBeNull();
    expect(parseConnString('   ')).toBeNull();
    expect(parseConnString(null)).toBeNull();
  });

  it('detects MySQL client', () => {
    const result = parseConnString('mysql -h 1.2.3.4 -P 3306 -u u -p p db');
    expect(result.db_type).toBe('mysql');
  });

  it('detects psql/postgres/pgsql', () => {
    expect(parseConnString('psql -h h -p 5432 -U u -d db').db_type).toBe('postgresql');
    expect(parseConnString('postgres -h h -p 5432 -U u -d db').db_type).toBe('postgresql');
    expect(parseConnString('pgsql -h h -p 5432 -U u -d db').db_type).toBe('postgresql');
  });

  it('detects mongo and sqlcmd clients', () => {
    expect(parseConnString('mongosh "mongodb://h:27017/db"').db_type).toBe('mongodb');
    expect(parseConnString('mongo --host h --port 27017 db').db_type).toBe('mongodb');
    expect(parseConnString('sqlcmd -S h -U u -P p -d db').db_type).toBe('sqlserver');
  });

  it('returns null when neither host nor db_type is recognizable', () => {
    expect(parseConnString('garbage')).toBeNull();
  });

  it('coerces port to Number', () => {
    const result = parseConnString('mysql -h h -P 3307 -u u -p p db');
    expect(result.port).toBe(3307);
  });

  it('lowercase -p is treated as PG port, not password', () => {
    const result = parseConnString('psql -h h -p 5432 -U u -d db');
    expect(result.port).toBe(5432);
    expect(result.password).toBeUndefined();
  });

  it('--key=value wins over -x VALUE when both are present', () => {
    // ``-h other`` vs ``--host=primary`` — --key=value takes precedence.
    const result = parseConnString('mysql --host=primary -h other -P 3306 -u u -p p db');
    expect(result.host).toBe('primary');
  });

  it('extracts database from positional argument', () => {
    const result = parseConnString('mysql -h h -P 3306 -u u --password=p mydb');
    expect(result.database_name).toBe('mydb');
  });
});

describe('parseSqlalchemyUrl — SQLAlchemy URL inputs', () => {
  it('handles mysql+pymysql URLs with URL-encoded password and query string', () => {
    // The exact failure case from the bug report: user pasted the
    // .env's EDIA_MYSQL_URL into the wizard and the password field
    // ended up showing the literal ``%40`` instead of ``@``.
    const result = parseSqlalchemyUrl(
      'mysql+pymysql://root:0Gg.B7c2%40tcX_jne6FMh@10.10.10.49:3306/aipdp_data_warehouse_prod?charset=utf8mb4'
    );
    expect(result).toEqual({
      db_type: 'mysql',
      host: '10.10.10.49',
      port: 3306,
      username: 'root',
      password: '0Gg.B7c2@tcX_jne6FMh',
      database_name: 'aipdp_data_warehouse_prod',
    });
  });

  it('parseConnString routes SQLAlchemy URLs through parseSqlalchemyUrl', () => {
    const result = parseConnString(
      'mysql+pymysql://root:0Gg.B7c2%40tcX_jne6FMh@10.10.10.49:3306/aipdp_data_warehouse_prod?charset=utf8mb4'
    );
    expect(result).toEqual({
      db_type: 'mysql',
      host: '10.10.10.49',
      port: 3306,
      username: 'root',
      password: '0Gg.B7c2@tcX_jne6FMh',
      database_name: 'aipdp_data_warehouse_prod',
    });
  });

  it('handles plain mysql:// without a driver suffix', () => {
    const result = parseSqlalchemyUrl('mysql://u:p@h:3306/d');
    expect(result).toEqual({
      db_type: 'mysql',
      host: 'h',
      port: 3306,
      username: 'u',
      password: 'p',
      database_name: 'd',
    });
  });

  it('handles postgresql URLs (with and without +psycopg2 driver)', () => {
    const pg = parseSqlalchemyUrl('postgresql://u:p@h:5432/d');
    const pgDriver = parseSqlalchemyUrl('postgresql+psycopg2://u:p@h:5432/d');
    expect(pg.db_type).toBe('postgresql');
    expect(pgDriver.db_type).toBe('postgresql');
    expect(pg.database_name).toBe('d');
  });

  it('handles mongodb and mongodb+srv URLs', () => {
    expect(
      parseSqlalchemyUrl('mongodb://u:p@h:27017/d').db_type
    ).toBe('mongodb');
    expect(
      parseSqlalchemyUrl('mongodb+srv://u:p@cluster.example.com/d').db_type
    ).toBe('mongodb');
    // SRV form has no port.
    const srv = parseSqlalchemyUrl('mongodb+srv://u:p@cluster.example.com/d');
    expect(srv.port).toBeUndefined();
    expect(srv.database_name).toBe('d');
  });

  it('handles SQL Server (mssql / sqlserver) URLs', () => {
    expect(parseSqlalchemyUrl('mssql://u:p@h:1433/d').db_type).toBe('sqlserver');
    expect(parseSqlalchemyUrl('sqlserver://u:p@h:1433/d').db_type).toBe(
      'sqlserver'
    );
  });

  it('URL-decodes the username and database name (not just the password)', () => {
    // Sanity check: every userinfo / path segment is decoded.
    const result = parseSqlalchemyUrl(
      'mysql+pymysql://user%23admin:p%40ss@host%2F1:3306/db%2Dname'
    );
    expect(result.username).toBe('user#admin');
    expect(result.password).toBe('p@ss');
    expect(result.host).toBe('host/1');
    expect(result.database_name).toBe('db-name');
  });

  it('coerces the port to Number', () => {
    const result = parseSqlalchemyUrl(
      'mysql+pymysql://u:p@host:3307/d'
    );
    expect(result.port).toBe(3307);
  });

  it('drops the trailing query string from the database name', () => {
    // sqlserver / mssql / postgresql users often tail the URL with
    // query params that contain the actual connection knobs.
    const result = parseSqlalchemyUrl(
      'mysql+pymysql://u:p@h:3306/d?charset=utf8mb4&ssl=true'
    );
    expect(result.database_name).toBe('d');
  });

  it('returns null for unknown / unsupported dialects', () => {
    expect(parseSqlalchemyUrl('sqlite:///tmp/file.db')).toBeNull();
    expect(parseSqlalchemyUrl('bigquery://project/dataset')).toBeNull();
    expect(parseSqlalchemyUrl('mysql -h h -u u -p p d')).toBeNull();
  });

  it('returns null for empty / malformed input', () => {
    expect(parseSqlalchemyUrl('')).toBeNull();
    expect(parseSqlalchemyUrl('not a url at all')).toBeNull();
    expect(parseSqlalchemyUrl('mysql+pymysql://')).toBeNull();
  });
});
