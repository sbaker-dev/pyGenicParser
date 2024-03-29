from .variantObjects import Variant
from . import errors_codes as ec
from . import misc as mc

from pathlib import Path
import numpy as np
import sqlite3
import struct
import zlib
import zstd


class BgenObject:
    def __init__(self, file_path, bgi_present=True, probability_return=None, probability=0.9, sample_path=None,
                 iid_index=slice(None, None, None), sid_index=slice(None, None, None),
                 ):
        """

        :param file_path:

        :param bgi_present: Takes a value of True if the .bgi is in the same directory and named file_path.bgi
            otherwise can ec passed as a path if it is in a different directory.
        :type bgi_present: bool | str

        :param iid_index: The default slice or np.ndarray that was create from getitem for the iid
        :type iid_index: slice | np.ndarray

        :param sid_index: The default slice or np.ndarray that was create from getitem for the sid
        :type sid_index: slice | np.ndarray

        :param probability:
        """

        # Construct paths
        self.file_path = Path(file_path)
        self._bgen_binary = open(file_path, "rb")
        self._sample_path = sample_path

        # Set indexers
        self.iid_index = iid_index
        self.sid_index = sid_index

        # Extract from header
        self._offset, self._headers_size, self._variant_number, self._sample_number, self._compression, \
            self._compressed, self._layout, self._sample_identifiers, self._variant_start = self._parse_header()

        # Index our sid and iid values if we have indexes, else the value is the same as variant/sample_number
        self.iid_count = len(np.arange(self._sample_number)[self.iid_index])
        self.sid_count = len(np.arange(self._variant_number)[self.sid_index])

        # Store numbers for altering functionality
        self._probability_return = probability_return
        self._probability = probability

        # Set the bgi file if present, and store this for indexing if required.
        self._bgi_present = bgi_present
        self._bgi_file = mc.set_bgi(self._bgi_present, self.file_path)
        if self._bgi_file:
            self._bgen_connection, self._bgen_index, self._last_variant_block = self._connect_to_bgi_index()
        else:
            self._bgen_connection, self._bgen_index, self._last_variant_block = None, None, None
        self._bgen_binary.close()

    def __repr__(self):
        return f"Bgen iid:sid -> {self.iid_count}:{self.sid_count}"

    def __getitem__(self, item):
        """Return a new BgenObject with slicing set."""
        # We always index on iid and sid so we need to have both
        assert len(item) == 2, ec.slice_error(type(item), len(item))
        iid_slicer, sid_slicer = item

        return BgenObject(self.file_path, self._bgi_present, self._probability_return, self._probability,
                          self._sample_path, self._set_slice(iid_slicer), self._set_slice(sid_slicer, False))

    def _set_slice(self, slice_object, iid=True):
        """
        Users may provide a slice or a list of indexes, for example from sid_to_index, so we need to set the indexes
        accordingly here

        :param slice_object: The slicing slice or list of indexes
        :type slice_object: slice | list

        :return: Numpy array of indexes
        :rtype: np.ndarray

        :raises TypeError: If the slicer is not a slice or list
        """

        if isinstance(slice_object, slice):
            if iid:
                return np.arange(self._sample_number)[slice_object]
            else:
                return np.arange(self._variant_number)[slice_object]

        elif isinstance(slice_object, (list, np.ndarray)):
            assert all([isinstance(index, (int, np.int32)) for index in slice_object]), ec.slice_list_type()

            # If failures are turned on in sid_to_index we will get negative indexes which we want to remove
            valid_values = [v for v in slice_object if v >= 0]
            if iid:
                return np.take(np.arange(self._sample_number), valid_values)
            else:
                return np.take(np.arange(self._variant_number), valid_values)

        else:
            raise TypeError(ec.wrong_slice_type(type(slice_object)))

    def sid_array(self):
        """Construct an array of all the snps that exist in this file"""
        assert self._bgen_index, ec.index_violation("sid_array")

        # Fetching all the seek positions
        self._bgen_index.execute("SELECT rsid FROM Variant")
        return np.array([name for name in self._bgen_index.fetchall()])[self.sid_index].flatten()

    def iid_array(self):
        """
        If sample identifiers are within the bgen file then these can be extracted and set, however this has not yet
        been tested and am unsure if sex and missing stored in bgen as is with .sample files?

        If a path to the samples has been provided, then we can load the information within it. Sample files contain
        both the FID and the IID as well as missing and sex allowing us more options akin to .fam files.

        If Nothing is provided, and nothing is embedded, we create a list of id's on then number of id's after indexing.


        :return: An array of id information
        """
        if self._sample_identifiers:
            return np.array(self._parse_sample_block())
        elif self._sample_path:
            raise NotImplementedError("Sorry this needs to be tested")
        else:
            return np.array([[i, i] for i in np.arange(self._sample_number)[self.iid_index]])

    def sid_to_index(self, snps, set_failed=False):
        """Convert a list of snps to a array of indexes"""
        # If we need to know which failed then we have to check first which snps exist within the full list, as
        # otherwise we may run into indexing problems, -1 being set for those not in the it and the name otherwise
        if set_failed:
            all_snps = self.sid_array()
            snp_check = [snp if snp in set(all_snps) else -1 for snp in snps]

            # Isolate the names we need to extract indexes for from the snps we checked
            snp_lookup = [snp for snp in snp_check if isinstance(snp, str)]

            # Construct a dict of snp: index for the ones we can lookup as they exist in all_snps, return the combined
            # lists
            snp_ids = {snp: i if snp in snp_lookup else -1 for i, snp in enumerate(all_snps[self.sid_index])}
            return np.array([snp_ids[snp] if isinstance(snp, str) else snp for snp in snp_check])

        # Otherwise just return the array of snps we find
        else:
            return np.array([i for i, snp in enumerate(self.sid_array()[self.sid_index]) if snp in set(snps)])

    def iid_to_index(self, iid_list, set_failed=False):
        """Isolate the iid indexes of the iid in the iid_list"""
        if set_failed:
            # todo implement it
            raise NotImplementedError("Sorry, set failed not yet implemented")
        else:
            iid_indexed = self.iid_array()[self.iid_index]
            iid_indexed_values = [self._index_idd(current_id, iid_indexed) for current_id in iid_list]
            return np.array([iid for iid in iid_indexed_values if iid or iid == 0])

    @staticmethod
    def _index_idd(current_id, iid_indexed):
        """
        Get the index of the current iid from the current indexed iid array

        :param current_id: The current idd we want the index position of
        :type current_id: np.ndarray

        :param iid_indexed: The iid_array after indexing that we calculated once and will now use for iteration
        :type iid_indexed: np.ndarray

        :return: The index
        :rtype: int
        """
        for i, iid in enumerate(iid_indexed):
            if all(iid == current_id):
                return i

    def info_array(self):
        """Return an array of all the variants in the bgen file"""
        assert self._bgen_index, ec.index_violation("info_array")

        self._bgen_index.execute("SELECT chromosome, position, rsid, allele1, allele2 FROM Variant")
        return np.array([Variant(chromosome, position, snp_id, a1, a2) for chromosome, position, snp_id, a1, a2
                         in self._bgen_index.fetchall()])[self.sid_index]

    def dosage_array(self):
        """Extract all the dosage information in the array"""
        assert self._bgen_index, ec.index_violation("dosage_array")
        self._bgen_binary = open(self.file_path, "rb")

        self._bgen_index.execute("SELECT file_start_position FROM Variant")
        dosage = np.array([self._get_variant(seek[0], True) for seek in self._bgen_index.fetchall()])[self.sid_index]
        self._bgen_binary.close()
        return dosage

    def variant_array(self):
        """Return an array of all the variants, where a variant is both the info + dosage"""
        assert self._bgen_index, ec.index_violation("variant_array")
        self._bgen_binary = open(self.file_path, "rb")

        self._bgen_index.execute("SELECT file_start_position FROM Variant")
        variants = np.array([self._get_variant(seek[0]) for seek in self._bgen_index.fetchall()],
                            dtype=object)

        self._bgen_binary.close()
        return self._index_variants(variants)

    def info_from_sid(self, snp_names):
        """Construct an array of variant identifiers for all the snps provided to snp_names"""
        assert self._bgen_index, ec.index_violation("variant_info_from_sid")

        # Select all the variants where the rsid is in the names provided
        if len(snp_names) > 1:
            self._bgen_index.execute(f"SELECT chromosome, position, rsid, allele1, allele2 FROM Variant"
                                     f" WHERE rsid IN {tuple(snp_names)}")
        elif len(snp_names) == 1:
            self._bgen_index.execute(f"SELECT chromosome, position, rsid, allele1, allele2 FROM Variant"
                                     f" WHERE rsid = '{snp_names[0]}'")
        else:
            print("No names passed - skipping")

        return np.array([Variant(chromosome, position, snp_id, a1, a2) for chromosome, position, snp_id, a1, a2
                         in self._bgen_index.fetchall()])[self.sid_index]

    def dosage_from_sid(self, snp_names):
        """Construct the dosage for all snps provide as a list or tuple of snp_names"""
        self._set_snp_names_file_positions(snp_names)

        dosage = np.array([self._get_variant(seek[0], True)[self.iid_index] for seek in self._bgen_index.fetchall()])
        self._bgen_binary.close()
        # todo This is causing errors in pgp, should we really be indexing on SID when we are taken a tuple of names?
        return dosage[self.sid_index]

    def variant_from_sid(self, snp_names):
        """Variant information for all snps within snp_names"""
        self._set_snp_names_file_positions(snp_names)
        variants = np.array([self._get_variant(seek[0]) for seek in self._bgen_index.fetchall()],
                            dtype=object)

        self._bgen_binary.close()
        return self._index_variants(variants)

    def _index_variants(self, variant):
        """Variants need to be indexed after fetch all as it returns a tuple of np.ndarrays"""
        return np.array([np.array((info, dosage[self.iid_index]), dtype=object) for info, dosage in variant]
                        )[self.sid_index]

    def _set_snp_names_file_positions(self, snp_names):
        """A tuple of 1 will lead to sql crashing if using IN, so we need to account for length 1 arrays"""
        assert self._bgen_index, ec.index_violation("dosage_from_sid")
        self._bgen_binary = open(self.file_path, "rb")

        # Select all the variants where the rsid is in the names provided
        if len(snp_names) > 1:
            self._bgen_index.execute(f"SELECT file_start_position FROM Variant WHERE rsid IN {tuple(snp_names)}")
        elif len(snp_names) == 1:
            self._bgen_index.execute(f"SELECT file_start_position FROM Variant WHERE rsid = '{snp_names[0]}'")
        else:
            print("No names passed - skipping")

    def _get_variant(self, seek, dosage=False):

        """
        Use the index of seek to move to the location of the variant in the file, then return the variant as Variant
        """
        self._bgen_binary.seek(seek)
        variant = self._get_curr_variant_info()

        if dosage:
            return self._get_curr_variant_data()
        else:
            return variant, self._get_curr_variant_data()

    def _get_curr_variant_info(self, as_list=False):
        """Gets the current variant's information."""

        if self._layout == 1:
            assert self._unpack("<I", 4) == self.iid_count, ec

        # Reading the variant id (may be in form chr1:8045045:A:G or just a duplicate of rsid and not used currently)
        self._read_bgen("<H", 2)

        # Reading the variant rsid
        rs_id = self._read_bgen("<H", 2)

        # Reading the chromosome
        chromosome = self._read_bgen("<H", 2)

        # Reading the position
        pos = self._unpack("<I", 4)

        # Getting the alleles
        alleles = [self._read_bgen("<I", 4) for _ in range(self._set_number_of_alleles())]

        # Return the Variant - currently only supports first two alleles
        if as_list:
            return [chromosome, pos, rs_id, alleles[0], alleles[1]]
        else:
            return Variant(chromosome, pos, rs_id, alleles[0], alleles[1])

    def _set_number_of_alleles(self):
        """
        Bgen version 2 can allow for more than 2 alleles, so if it is version 2 then unpack the number stored else
        return 2
        :return: number of alleles for this snp
        :rtype: int
        """
        if self._layout == 2:
            return self._unpack("<H", 2)
        else:
            return 2

    def _get_curr_variant_data(self):
        """Gets the current variant's dosage or probabilities."""

        if self._layout == 1:
            print("WARNING - UNTESTED CODE FROM PY-BGEN")
            # Getting the probabilities
            probs = self._get_curr_variant_probs_layout_1()

            if self._probability_return:
                # Returning the probabilities
                return probs

            else:
                # Returning the dosage
                return self._layout_1_probs_to_dosage(probs)

        else:
            # Getting the probabilities
            probs, missing_data = self._get_curr_variant_probs_layout_2()

            if self._probability_return:
                # Getting the alternative allele homozygous probabilities
                last_probs = self._get_layout_2_last_probs(probs)

                # Stacking the probabilities
                last_probs.shape = (last_probs.shape[0], 1)
                full_probs = np.hstack((probs, last_probs))

                # Setting the missing to NaN
                full_probs[missing_data] = np.nan

                # Returning the probabilities
                return full_probs

            else:
                # Computing the dosage
                dosage = self._layout_2_probs_to_dosage(probs)

                # Setting the missing to NaN
                dosage[missing_data] = np.nan

                # Returning the dosage
                return dosage

    def _get_curr_variant_probs_layout_1(self):
        """Gets the current variant's probabilities (layout 1)."""
        c = self._sample_number
        if self._compressed:
            c = self._unpack("<I", 4)

        # Getting the probabilities
        probs = np.frombuffer(
            self._compression(self._bgen_binary.read(c)),
            dtype="u2",
        ) / 32768
        probs.shape = (self._sample_number, 3)

        return probs

    def _layout_1_probs_to_dosage(self, probs):
        """Transforms probability values to dosage (from layout 1)"""
        # Constructing the dosage
        dosage = 2 * probs[:, 2] + probs[:, 1]
        if self._probability > 0:
            dosage[~np.any(probs >= self._probability, axis=1)] = np.nan

        return dosage

    def _get_curr_variant_probs_layout_2(self):
        """Gets the current variant's probabilities (layout 2)."""
        # The total length C of the rest of the data for this variant
        c = self._unpack("<I", 4)

        # The number of bytes to read
        to_read = c

        # D = C if no compression
        d = c
        if self._compressed:
            # The total length D of the probability data after
            # decompression
            d = self._unpack("<I", 4)
            to_read = c - 4

        # Reading the data and checking
        data = self._compression(self._bgen_binary.read(to_read))
        assert len(data) == d, "INVALID HERE"

        # Checking the number of samples
        n = mc.struct_unpack("<I", data[:4])
        assert n == self._sample_number, ec.sample_size_violation(self._sample_number, n)

        data = data[4:]

        # Checking the number of alleles (we only accept 2 alleles)
        nb_alleles = mc.struct_unpack("<H", data[:2])
        assert nb_alleles == 2, "INVALID HERE"
        data = data[2:]

        # TODO: Check ploidy for sexual chromosomes
        # The minimum and maximum for ploidy (we only accept ploidy of 2)
        min_ploidy = mc.byte_to_int(data[0])
        max_ploidy = mc.byte_to_int(data[1])
        if min_ploidy != 2 and max_ploidy != 2:
            raise ValueError("INVALID HERE")

        data = data[2:]

        # Check the list of N bytes for missingness (since we assume only
        # diploid values for each sample)
        ploidy_info = np.frombuffer(data[:n], dtype=np.uint8)
        ploidy_info = np.unpackbits(
            ploidy_info.reshape(1, ploidy_info.shape[0]).T,
            axis=1,
        )
        missing_data = ploidy_info[:, 0] == 1
        data = data[n:]

        # TODO: Permit phased data
        # Is the data phased?
        is_phased = data[0] == 1
        if is_phased:
            raise ValueError(
                "{}: only accepting unphased data".format("INVALID")
            )
        data = data[1:]

        # The number of bits used to encode each probabilities
        b = mc.byte_to_int(data[0])
        data = data[1:]

        # Reading the probabilities (don't forget we allow only for diploid
        # values)
        if b == 8:
            probs = np.frombuffer(data, dtype=np.uint8)

        elif b == 16:
            probs = np.frombuffer(data, dtype=np.uint16)

        elif b == 32:
            probs = np.frombuffer(data, dtype=np.uint32)

        else:
            probs = mc.pack_bits(data, b)

        # Changing shape and computing dosage
        probs.shape = (self._sample_number, 2)

        return probs / (2 ** b - 1), missing_data

    @staticmethod
    def _get_layout_2_last_probs(probs):
        """
        Gets the layout 2 last probabilities (homo alternative).
        :rtype: np.ndarray
        """
        return 1 - np.sum(probs, axis=1)

    def _layout_2_probs_to_dosage(self, probs):
        """Transforms probability values to dosage (from layout 2)."""
        # Computing the last genotype's probabilities
        last_probs = self._get_layout_2_last_probs(probs)

        # Constructing the dosage
        dosage = 2 * last_probs + probs[:, 1]

        # Setting low quality to NaN
        if self._probability > 0:
            good_probs = (
                    np.any(probs >= self._probability, axis=1) |
                    (last_probs >= self._probability)
            )
            dosage[~good_probs] = np.nan

        return dosage

    def _parse_header(self):
        """
        Extract information from the header of the bgen file.

        Spec at https://www.well.ox.ac.uk/~gav/bgen_format/spec/latest.html

        :return: offset, headers, variant_number, sample_number, compression, layout, and sample_identifiers
        """

        # Check the header block is not larger than offset
        offset = self._unpack("<I", 4)
        headers_size = self._unpack("<I", 4)
        assert headers_size <= offset, ec.offset_violation(self._bgen_binary.name, offset, headers_size)
        variant_start = offset + 4

        # Extract the number of variants and samples
        variant_number = self._unpack("<I", 4)
        sample_number = self._unpack("<I", 4)

        # Check the file is valid
        magic = self._unpack("4s", 4)
        assert (magic == b'bgen') or (struct.unpack("<I", magic)[0] == 0), ec.magic_violation(self._bgen_binary.name)

        # Skip the free data area
        self._bgen_binary.read(headers_size - 20)

        # Extract the flag, then set compression layout and sample identifiers from it
        compression, compressed, layout, sample_identifiers = self._header_flag()
        return (offset, headers_size, variant_number, sample_number, compression, compressed, layout,
                sample_identifiers, variant_start)

    def _header_flag(self):
        """
        The flag represents a 4 byte unsigned int, where the bits relates to the compressedSNPBlock at bit 0-1, Layout
        at 2-5, and sampleIdentifiers at 31

        Spec at https://www.well.ox.ac.uk/~gav/bgen_format/spec/latest.html

        :return: Compression, layout, sampleIdentifiers
        """
        # Reading the flag
        flag = np.frombuffer(self._bgen_binary.read(4), dtype=np.uint8)
        flag = np.unpackbits(flag.reshape(1, flag.shape[0]), bitorder="little")

        # [N1] Bytes are stored right to left hence the reverse, see shorturl.at/cOU78
        # Check the compression of the data
        compression_flag = mc.bits_to_int(flag[0: 2][::-1])
        assert 0 <= compression_flag < 3, ec.compression_violation(self._bgen_binary.name, compression_flag)
        if compression_flag == 0:
            compressed = False
            compression = mc.no_decompress
        elif compression_flag == 1:
            compressed = True
            compression = zlib.decompress
        else:
            compressed = True
            compression = zstd.decompress

        # Check the layout is either 1 or 2, see [N1]
        layout = mc.bits_to_int(flag[2:6][::-1])
        assert 1 <= layout < 3, ec.layout_violation(self._bgen_binary.name, layout)

        # Check if the sample identifiers are in the file or not, then return
        assert flag[31] == 0 or flag[31] == 1, ec.sample_identifier_violation(self._bgen_binary.name, flag[31])
        if flag[31] == 0:
            return compression, compressed, layout, False
        else:
            return compression, compressed, layout, True

    def _parse_sample_block(self):
        """Parses the sample block."""
        self._bgen_binary = open(self.file_path, "rb")
        self._parse_header()

        # Getting the block size
        block_size = self._unpack("<I", 4)
        assert block_size + self._headers_size == self._offset, ec.sample_block_violation(
            self._headers_size, self._offset, block_size)

        # Checking the number of samples
        n = self._unpack("<I", 4)
        assert n == self._sample_number, ec.sample_size_violation(self._sample_number, n)

        # Getting the sample information
        samples = [self._read_bgen("<H", 2) for _ in range(self._sample_number)]

        # Check the samples extract are equal to the number present then return
        self._bgen_binary.close()
        assert len(samples) == self._sample_number, ec.sample_size_violation(self._sample_number, len(samples))
        return samples

    def _connect_to_bgi_index(self):
        """Connect to the index (which is an SQLITE database)."""
        bgen_file = sqlite3.connect(str(self.file_path.absolute()) + ".bgi")
        bgen_index = bgen_file.cursor()

        # Fetching the number of variants and the first and last seek position
        bgen_index.execute(
            "SELECT COUNT (rsid), "
            "       MIN (file_start_position), "
            "       MAX (file_start_position) "
            "FROM Variant"
        )
        nb_markers, first_variant_block, last_variant_block = bgen_index.fetchone()

        # Check the number of markers are the same across bgen and bgi, and that they start in the same block
        assert nb_markers == self._variant_number, ec
        assert first_variant_block == self._variant_start

        # Checking the number of markers
        if nb_markers != self._variant_number:
            raise ValueError("Number of markers different between headers of bgen and bgi")

        # Checking the first variant seek position
        if first_variant_block != self._variant_start:
            raise ValueError(f"{self.file_path.name}: invalid index")

        return bgen_file, bgen_index, last_variant_block

    def create_bgi(self, bgi_write_path=None):
        """
        Mimic bgenix .bgi via python

        Note
        -----
        This does not re-create the meta file data that bgenix makes, so if you are using this to make a bgi for another
        process that is going to validate that then it won't work.

        bgenix: https://enkre.net/cgi-bin/code/bgen/wiki/bgenix
        """

        # This only works on bgen version 1.2
        assert self._layout == 2

        # Check if the file already exists
        if not bgi_write_path:
            write_path = str(self.file_path.absolute()) + ".bgi"
        else:
            write_path = str(Path(bgi_write_path, self.file_path.name).absolute()) + ".bgi"

        if Path(write_path).exists():
            print(f"Bgi Already exists for {self.file_path.name}")
        else:
            # Establish the connection
            connection = sqlite3.connect(write_path)
            c = connection.cursor()

            # Create our core table that mimics Variant bgi from bgenix
            c.execute('''
                 CREATE TABLE Variant (
                 file_start_position INTEGER,
                 size_in_bytes INTEGER,
                 chromosome INTEGER,
                 position INTEGER,
                 rsid TEXT,
                 allele1 TEXT,
                 allele2 TEXT
                   )''')

            # Write values to table
            self._bgen_binary = open(self.file_path, "rb")
            self._bgen_binary.seek(self._variant_start)
            for value in [self._set_bgi_lines() for _ in range(self.sid_count)]:
                c.execute(f'INSERT INTO Variant VALUES {tuple(value)}')

            # Commit the file
            connection.commit()
            connection.close()
            self._bgen_binary.close()

    def _set_bgi_lines(self):
        """This will extract a given start position of the dosage, the size of the dosage, and the variant array"""
        # Isolate the block start position
        start_position = self._bgen_binary.tell()

        # Extract the Bim information, Then append the start position and then bim information
        variant = self._get_curr_variant_info(as_list=True)

        # Get the dosage size, then skip this size + the current position to get the position of the next block
        dosage_size = self._unpack("<I", 4)

        # Calculate the variant size in bytes
        size_in_bytes = (self._bgen_binary.tell() - start_position) + dosage_size

        # Append this information to lines, then seek past the dosage block
        self._bgen_binary.seek(self._bgen_binary.tell() + dosage_size)
        return [start_position, size_in_bytes] + variant

    def _read_bgen(self, struct_format, size):
        """
        Sometimes we need to read the number of bytes read via unpack

        :param struct_format: The string representation of the format to use in struct format. See struct formatting for
            a list of example codes.
        :type struct_format: str

        :param size: The byte size
        :type size: int

        :return: Decoded bytes that where read
        """
        return self._bgen_binary.read(self._unpack(struct_format, size)).decode()

    # todo: Update to use miscSupports instead
    def _unpack(self, struct_format, size, list_return=False):
        """
        Use a given struct formatting to unpack a byte code

        Struct formatting
        ------------------
        https://docs.python.org/3/library/struct.html

        :param struct_format: The string representation of the format to use in struct format. See struct formatting for
            a list of example codes.
        :type struct_format: str

        :param size: The byte size
        :type size: int

        :key list_return: If we expect multiple values then we return a tuple of what was unpacked, however if there is
            only one element then we often just index the first element to return it directly. Defaults to false.
        :type list_return: bool

        :return: Whatever was unpacked
        :rtype: Any
        """
        if list_return:
            return struct.unpack(struct_format, self._bgen_binary.read(size))
        else:
            return struct.unpack(struct_format, self._bgen_binary.read(size))[0]
