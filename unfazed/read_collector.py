#! /usr/bin/env python
from __future__ import print_function

# Python 2/3 compatibility
import sys

import numpy as np
import pysam

from .site_searcher import binary_search

MILLION = 1000000
MIN_MAPQ = 1
STDEV_COUNT = 3
READLEN = 151
SPLITTER_ERR_MARGIN = 5
EXTENDED_RB_READ_GOAL = 100


def estimate_concordant_insert_len(bamfile):
    insert_sizes = []
    for i, read in enumerate(bamfile):
        insert = abs(read.tlen-(READLEN*2))
        insert_sizes.append(insert)

        if i >= MILLION:
            break
    insert_sizes = np.array(insert_sizes)
    # filter out the top .1% as weirdies
    insert_sizes = np.percentile(insert_sizes, 99.5)

    frag_len = int(np.mean(insert_sizes))
    stdev = np.std(insert_sizes)
    concordant_size = frag_len + (stdev * STDEV_COUNT)
    return concordant_size


def goodread(read):
    if not read:
        return False
    if (
        read.is_qcfail
        or read.is_unmapped
        or read.is_duplicate
        or int(read.mapping_quality) < MIN_MAPQ
        or read.is_secondary
        or read.is_supplementary
        or read.mate_is_unmapped
        or (read.next_reference_id != read.reference_id)
    ):
        return False
    return True


def get_allele_at(read, mate, pos):
    read_ref_positions = read.get_reference_positions(full_length=True)
    if mate:
        mate_ref_positions = mate.get_reference_positions(full_length=True)

    if pos in read_ref_positions:
        read_pos = read_ref_positions.index(pos)
        return read.query_sequence[read_pos]
    elif mate and pos in mate_ref_positions:
        mate_pos = mate_ref_positions.index(pos)
        return mate.query_sequence[mate_pos]
    else:
        return False


def connect_reads(grouped_readsets, read_sites, site_reads, new_reads, fetched_reads):
    """
    """
    reads_to_add = {"ref": [], "alt": []}
    for haplotype in new_reads:
        other_haplotype = "ref" if haplotype == "alt" else "alt"
        for readname, found_pos in new_reads[haplotype]:
            if readname not in read_sites:
                # if the read isn't in the read sites collection it's from the original
                # variant and didn't overlap any het sites
                continue
            for site in read_sites[readname]:
                # we don't want to reprocess the same site where we found the read originally
                # reads from the breakpoints of dnmsv have -1 as pos
                if site["pos"] == found_pos:
                    continue
                finder_allele = get_allele_at(
                    fetched_reads[readname][0], fetched_reads[readname][1], site["pos"],
                )
                non_finder_allele = None
                if finder_allele:
                    if finder_allele == site["ref_allele"]:
                        non_finder_allele = site["alt_allele"]
                    elif finder_allele == site["alt_allele"]:
                        non_finder_allele = site["ref_allele"]

                if not (finder_allele and non_finder_allele):
                    continue
                for site_readname in site_reads[site["pos"]]:
                    # if we haven't already found the reads in the next_site, add them
                    if (site_readname not in grouped_readsets["ref"]) and (
                        site_readname not in grouped_readsets["alt"]
                    ):
                        # we need to assign this read to a haplotype
                        # either the same hp as the de novo or not
                        read, mate = fetched_reads[site_readname]
                        new_read_allele = get_allele_at(read, mate, site["pos"])

                        if not new_read_allele:
                            continue

                        # if the read's allele at the het site matches the allele
                        # in the read we used to find it,
                        # this read belong to the current haplotype.
                        # If it matches the non_finder allele it belong to the other haplotype
                        # otherwise it's an error
                        if new_read_allele == finder_allele:
                            reads_to_add[haplotype].append([site_readname, site["pos"]])
                            grouped_readsets[haplotype].add(site_readname)
                        elif new_read_allele == non_finder_allele:
                            reads_to_add[other_haplotype].append(
                                [site_readname, site["pos"]]
                            )
                            grouped_readsets[other_haplotype].add(site_readname)

    if len(reads_to_add["alt"]) + len(reads_to_add["ref"]) > 0:
        grouped_readsets = connect_reads(
            grouped_readsets, read_sites, site_reads, reads_to_add, fetched_reads,
        )

    return grouped_readsets


def group_reads_by_haplotype(bamfile, region, grouped_reads, het_sites, reads_idx):
    """
    using the heterozygous sites to group the reads into those which come from the same
    haplotype as the de novo variant (alt) and those that don't (ref)
    """
    fetched_reads = {}
    read_sites = {}
    site_reads = {}
    for het_site in het_sites:
        try:
            bam_iter = bamfile.fetch(
                region["chrom"], het_site["pos"], het_site["pos"] + 1
            )
        except ValueError:
            chrom = (
                region["chrom"].strip("chr")
                if "chr" in region["chrom"]
                else "chr" + region["chrom"]
            )
            bam_iter = bamfile.fetch(chrom, het_site["pos"], het_site["pos"] + 1)

        for i, read in enumerate(bam_iter):
            if i > EXTENDED_RB_READ_GOAL:
                continue
            if goodread(read):
                try:
                    mate = bamfile.mate(read)
                except ValueError:
                    continue
                if goodread(mate):
                    read_coords = [read.reference_start, read.reference_end]
                    mate_coords = [mate.reference_start, mate.reference_end]
                    if (
                        mate_coords[0] <= read_coords[0] <= mate_coords[1]
                        or mate_coords[0] <= read_coords[1] <= mate_coords[1]
                    ):
                        # this means the mate pairs overlap each other,
                        # which is not biologically possible
                        # and is a sign of an alignment error
                        continue
                    if read.query_name not in read_sites:
                        read_sites[read.query_name] = []
                    if het_site["pos"] not in site_reads:
                        site_reads[het_site["pos"]] = []

                    read_sites[read.query_name].append(het_site)
                    site_reads[het_site["pos"]].append(read.query_name)
                    fetched_reads[read.query_name] = [read, mate]
    grouped_readsets = {"ref": set(), "alt": set()}
    new_reads = {"alt": [], "ref": []}
    for read in grouped_reads["alt"]:
        # all the reads we have so far come from the alt allele (the dnm)
        # so store them as a set in that index
        # also make a list of readnames and nonreal positions for the matching algorithm
        grouped_readsets["alt"].add(read.query_name)
        new_reads["alt"].append([read.query_name, -1])
        try:
            mate = bamfile.mate(read)
            fetched_reads[read.query_name] = [read, mate]
            match_sites = binary_search(
                read.reference_start, read.reference_end, het_sites
            )
            if len(match_sites) <= 0:
                continue
            if read.query_name not in read_sites:
                read_sites[read.query_name] = []
            if het_site["pos"] not in site_reads:
                site_reads[het_site["pos"]] = []

            for match_site in match_sites:
                read_sites[read.query_name].append(match_site)
                site_reads[het_site["pos"]].append(read.query_name)
        except ValueError:
            continue

    connected_reads = connect_reads(
        grouped_readsets, read_sites, site_reads, new_reads, fetched_reads
    )
    extended_grouped_reads = {"ref": [], "alt": []}
    # for haplotype in connected_reads:
    for haplotype in connected_reads:
        for readname in connected_reads[haplotype]:
            if readname not in fetched_reads:
                continue
            readpair = fetched_reads[readname]
            for read in readpair:
                extended_grouped_reads[haplotype].append(read)
    return extended_grouped_reads


def collect_reads_snv(
    bam_name, region, het_sites, ref, alt, cram_ref, no_extended, concordant_upper_len=None
):
    """
    given an alignment file name, a de novo SNV region,
    and a list of heterozygous sites for haplotype grouping,
    return the reads that support the variant as a dictionary with two lists,
    containing reads that support and reads that don't
    """
    if "cram" == bam_name[-4:]:
        bamfile = pysam.AlignmentFile(bam_name, "rc", reference_filename=cram_ref)
    else:
        bamfile = pysam.AlignmentFile(bam_name, "rb")

    if not concordant_upper_len:
        concordant_upper_len = estimate_concordant_insert_len(bamfile)

    supporting_reads = []
    position = int(region["start"])
    try:
        bam_iter = bamfile.fetch(region["chrom"], position - 1, position + 1)
    except ValueError:
        chrom = (
            region["chrom"].strip("chr")
            if "chr" in region["chrom"]
            else "chr" + region["chrom"]
        )
        bam_iter = bamfile.fetch(chrom, position, position + 1)
    informative_reads = {"alt": supporting_reads, "ref": []}
    for read in bam_iter:
        if not goodread(read):
            continue
        # find mate for informative site check
        try:
            mate = bamfile.mate(read)
            if not goodread(mate):
                continue
        except ValueError:
            continue
        ref_positions = read.get_reference_positions(full_length=True)
        mate_ref_positions = mate.get_reference_positions(full_length=True)
        if ((ref_positions.count(None) > 5) or (mate_ref_positions.count(None) > 5)):
            continue

        # find reads that support the alternate allele and reads that don't
        read_allele = get_allele_at(read, mate, position)
        if read_allele == ref:
            informative_reads["ref"].append(read)
            if mate:
                informative_reads["ref"].append(mate)
        elif read_allele == alt:
            informative_reads["alt"].append(read)
            if mate:
                informative_reads["alt"].append(mate)
    if no_extended:
        return informative_reads
    informative_reads = group_reads_by_haplotype(bamfile, region, informative_reads, het_sites, 0)
    return informative_reads


def collect_reads_sv(bam_name, region, het_sites, cram_ref, no_extended, concordant_upper_len=None):
    """
    given an alignment file name, a de novo SV region,
    and a list of heterozygous sites for haplotype grouping,
    return the reads that support the variant as a dictionary with two lists,
    containing reads that support and reads that don't
    """
    if "cram" == bam_name[-4:]:
        bamfile = pysam.AlignmentFile(bam_name, "rc", reference_filename=cram_ref)
    else:
        bamfile = pysam.AlignmentFile(bam_name, "rb")

    if not concordant_upper_len:
        concordant_upper_len = estimate_concordant_insert_len(bamfile)

    supporting_reads = []
    var_len = abs(float(region["end"]) - float(region["start"]))
    for position in region["start"], region["end"]:
        position = int(position)
        try:
            bam_iter = bamfile.fetch(
                region["chrom"],
                max(0, position - concordant_upper_len),
                position + concordant_upper_len,
            )
        except ValueError:
            chrom = region["chrom"]
            if "chr" in chrom:
                chrom = chrom.replace("chr", "")
            else:
                chrom = "chr" + chrom

            bam_iter = bamfile.fetch(
                chrom, max(0, position - concordant_upper_len), position + concordant_upper_len,
            )
        for read in bam_iter:
            if not goodread(read):
                continue

            # find mate for informative site check
            try:
                mate = bamfile.mate(read)
            except ValueError:
                continue
            insert_size = abs(read.tlen-(READLEN*2))
            if not goodread(mate):
                continue

            if read.has_tag("SA"):
                # if it's a splitter and the clipping begins within
                # SPLITTER_ERR_MARGIN bases of the breaks, keep it
                if ((position - SPLITTER_ERR_MARGIN) <= read.reference_start <= (position + SPLITTER_ERR_MARGIN) 
                    or (position - SPLITTER_ERR_MARGIN) <= read.reference_end <= (position + SPLITTER_ERR_MARGIN)
                ):
                    supporting_reads.append(read)
                    supporting_reads.append(mate)
            elif ((insert_size > concordant_upper_len)
                    and (0.7 < abs(var_len / insert_size) < 1.3)):
                # find mate for informative site check
                try:
                    mate = bamfile.mate(read)
                except ValueError:
                    continue
                left_read_positions = [
                    min(mate.reference_start, read.reference_start),
                    min(mate.reference_end,read.reference_end)
                ]
                right_read_positions = [
                    max(mate.reference_start, read.reference_start),
                    max(mate.reference_end,read.reference_end)
                ]
                concordant_with_wiggle = int(concordant_upper_len)
                if not ((region['start']-concordant_with_wiggle) < left_read_positions[0] < (region['start']+concordant_with_wiggle)
                        and (region['end']-concordant_with_wiggle) < right_read_positions[0] < (region['end']+concordant_with_wiggle)):
                    continue

                supporting_reads.append(mate)
                supporting_reads.append(read)
            else: #find clipped reads that aren't split alignment but still support the variant
                ref_positions = read.get_reference_positions(full_length=True)
                if position in ref_positions:
                    region_pos = ref_positions.index(position)
                elif position-1 in ref_positions:
                    region_pos = ref_positions.index(position-1)
                elif position+1 in ref_positions:
                    region_pos = ref_positions.index(position+1)
                else:
                    continue
                if (region_pos < 2 ) or (region_pos > (len(ref_positions)-4)):
                    continue
                before_positions = list(set(ref_positions[:region_pos-1]))
                after_positions = list(set(ref_positions[region_pos+1:]))
                #identify clipping that matches the variant
                if (len(before_positions) == 1 and before_positions[0] is None or 
                    len(after_positions) == 1 and after_positions[0] is None):
                    supporting_reads.append(mate)
                    supporting_reads.append(read)
    informative_reads = {"alt": supporting_reads, "ref": []}
    if no_extended:
        return informative_reads
    informative_reads = group_reads_by_haplotype(bamfile, region, informative_reads, het_sites, 0)
    return informative_reads


if __name__ == "__main__":
    sys.exit("Import this as a module")
